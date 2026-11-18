from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Reply
from astrbot.api.message_components import Image as AstrImage
from astrbot.api.star import Context, Star
from PIL import Image as PILImage

_PLUGIN_NAME = "astrbot_plugin_ai_image"
# Some adapters use placeholder IDs instead of a real user. Never treat these
# as @-targets (QQ official C2C/group adapters use "qq_official" / openids).
_AT_SENTINELS = {"all", "qq_official", "unknown_selfid"}
_MEDIA_PREFIX = (
    r"(?:(?:\[[^\]]+\]|<@!?[^>\s]+>|<qqbot-at-user\s+[^>]*/>)\s*)*"
)
_GEN_RE = re.compile(rf"^\s*{_MEDIA_PREFIX}/?(?:生图|生成图片|画图)\s*(.*?)\s*$", re.S)
_EDIT_RE = re.compile(rf"^\s*{_MEDIA_PREFIX}/?(?:改图|图片二创|编辑图片)\s*(.*?)\s*$", re.S)
_MODELS_RE = re.compile(r"^/?(?:生图模型|图片模型|AI模型)\s*$", re.I)
_CQ_IMAGE_RE = re.compile(r"\[CQ:image,([^\]]+)\]", re.I)


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if value is None:
        return None
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        try:
            dumped = dumper()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return getattr(value, "__dict__", None)


def _find_image_urls(value: Any, seen: set[int] | None = None) -> list[str]:
    """从 AstrBot/OneBot 消息链中递归提取图片 URL 或本地路径。"""
    seen = seen or set()
    if value is None or id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, str):
        found: list[str] = []
        if value.startswith(("http://", "https://", "file://", "/")):
            found.append(value)
        for segment in _CQ_IMAGE_RE.findall(value):
            fields = dict(item.split("=", 1) for item in segment.split(",") if "=" in item)
            candidate = fields.get("url") or fields.get("file")
            if candidate:
                found.append(candidate)
        return found
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_find_image_urls(item, seen))
        return result
    data = _as_dict(value)
    if not data:
        return []
    result = []
    typ = str(data.get("type", data.get("_type", ""))).lower()
    class_name = type(value).__name__.lower()
    if typ in {"image", "img", "componenttype.image"} or typ.endswith(".image") or class_name == "image":
        for key in ("url", "file", "path", "src"):
            candidate = data.get(key)
            if isinstance(candidate, str) and candidate:
                result.append(candidate)
        nested = data.get("data")
        nested_data = _as_dict(nested)
        if nested_data:
            for key in ("url", "file", "path", "src"):
                candidate = nested_data.get(key)
                if isinstance(candidate, str) and candidate:
                    result.append(candidate)
    for key, child in data.items():
        if key not in {"url", "file", "path", "src"}:
            result.extend(_find_image_urls(child, seen))
    return list(dict.fromkeys(result))


def _find_at_ids(value: Any, seen: set[int] | None = None) -> list[str]:
    """从消息链递归提取 QQ At 组件中的用户 ID。"""
    seen = seen or set()
    if value is None or id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, (list, tuple, set)):
        return [qq for child in value for qq in _find_at_ids(child, seen)]
    data = _as_dict(value)
    if not data:
        return []
    typ = str(data.get("type", data.get("_type", ""))).lower()
    class_name = type(value).__name__.lower()
    result: list[str] = []
    if (typ.endswith(".at") or typ in {"at", "componenttype.at"} or class_name == "at") and str(
        data.get("qq", data.get("user_id", ""))
    ) != "all":
        qq = str(data.get("qq", data.get("user_id", ""))).strip()
        if not qq:
            nested_data = _as_dict(data.get("data"))
            qq = str((nested_data or {}).get("qq") or (nested_data or {}).get("user_id", "")).strip()
        if qq:
            result.append(qq)
    for key, child in data.items():
        if key not in {"qq", "name"}:
            result.extend(_find_at_ids(child, seen))
    return list(dict.fromkeys(result))


class ImageRequestRejected(Exception):
    """The request itself was rejected; another provider must not retry it."""


class AiImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._client: httpx.AsyncClient | None = None
        self._temp_paths: set[str] = set()
        self._models_cache: list[str] | None = None
        self._request_profile: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            "ai_image_request_profile", default=None
        )
        self._request_slots: asyncio.Semaphore | None = None
        self._request_slots_count = 0

    def _cfg(self, key: str, default: Any = "") -> Any:
        profiles = self._profiles()
        active = str(self.config.get("default_provider", "") or "").strip()
        profile = self._request_profile.get()
        if profile and profile in profiles:
            return profiles[profile].get(key, self.config.get(key, default))
        if active and active in profiles:
            return profiles[active].get(key, self.config.get(key, default))
        return self.config.get(key, default)

    def _profile_names(self) -> list[str]:
        return list(self._profiles())

    def _profiles(self) -> dict[str, Any]:
        providers = self.config.get("api_providers", [])
        if isinstance(providers, list):
            result = {}
            for item in providers:
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                # Native template_list uses base_url/api_keys; normalize to the
                # legacy fields consumed by the request implementation.
                value = dict(item)
                value.setdefault("api_base", value.get("base_url", ""))
                keys = value.get("api_keys", [])
                value.setdefault("api_key", keys[0] if isinstance(keys, list) and keys else "")
                result[str(item["name"])] = value
            return result
        profiles = self.config.get("profiles", {})
        if isinstance(profiles, str):
            try:
                profiles = json.loads(profiles)
            except (TypeError, ValueError):
                profiles = {}
        return profiles if isinstance(profiles, dict) else {}

    def _save_config(self) -> None:
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            saver()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = max(10.0, float(self._cfg("timeout_sec", 180) or 180))
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
                follow_redirects=True,
            )
        return self._client

    async def _send_progress(self, event: AstrMessageEvent, message: str) -> None:
        """发送即时进度，不 yield，避免中断当前异步处理器。"""
        try:
            result = event.plain_result(message)
            try:
                await event.send(result)
            except (AttributeError, TypeError):
                chain = getattr(result, "chain", None)
                if chain is None:
                    raise
                await event.send(chain)
        except Exception as exc:
            logger.warning(f"[{_PLUGIN_NAME}] 发送进度提示失败: {exc}")

    @staticmethod
    def _rejection_from_payload(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        details = error if isinstance(error, dict) else payload
        code = details.get("code", payload.get("code"))
        if str(code) != "400":
            return None
        return str(details.get("message") or payload.get("message") or "您的请求无法用于生成图像。该请求可能因安全政策被拦截，或不适合进行图像生成。").strip()

    @staticmethod
    def _rejection_message(exc: Exception) -> str | None:
        if isinstance(exc, ImageRequestRejected):
            return str(exc)
        if not isinstance(exc, httpx.HTTPStatusError):
            return None
        try:
            return AiImagePlugin._rejection_from_payload(exc.response.json())
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _error_detail(exc: Exception) -> str:
        if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
            return "生图服务器请求超时，请稍后重试。"
        if isinstance(exc, httpx.ConnectError):
            return "生图服务器连接失败，请检查 API 地址或服务状态。"
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                payload = exc.response.json()
                error = payload.get("error", payload) if isinstance(payload, dict) else {}
                if isinstance(error, dict):
                    message = str(error.get("message") or "").strip()
                    if message:
                        return message
                    error_type = str(error.get("type") or error.get("code") or "").strip()
                    if error_type:
                        return error_type
            except (ValueError, TypeError):
                pass
            return f"上游请求失败（HTTP {exc.response.status_code}）"
        if isinstance(exc, httpx.HTTPError):
            return "生图服务器网络请求失败。"
        return str(exc) or "上游请求失败"

    @staticmethod
    def _api_error(exc: Exception) -> str:
        return AiImagePlugin._error_detail(exc)

    def _headers(self) -> dict[str, str]:
        key = str(self._cfg("api_key", "") or "").strip()
        if not key:
            raise RuntimeError("未配置 API Key，请在插件配置中填写")
        if str(self._cfg("provider", "openai")).lower() == "claude":
            return {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        return {"authorization": f"Bearer {key}", "content-type": "application/json"}

    def _base(self) -> str:
        return str(self._cfg("api_base", "") or "https://api.openai.com/v1").rstrip("/")

    async def _list_models(self, refresh: bool = False) -> list[str]:
        """读取 OpenAI/Anthropic 兼容网关的模型列表。"""
        if self._models_cache is not None and not refresh:
            return self._models_cache
        client = await self._http()
        provider = str(self._cfg("provider", "openai")).lower()
        endpoint = f"{self._base().rstrip('/')}/models"
        if provider == "claude" and not self._base().endswith("/v1"):
            endpoint = f"{self._base()}/v1/models"
        response = await client.get(endpoint, headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        models = []
        for item in payload.get("data", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
        self._models_cache = list(dict.fromkeys(models))
        return self._models_cache

    async def _model(self) -> str:
        configured = str(self._cfg("model", "") or "").strip()
        if configured and configured.lower() != "auto":
            return configured
        provider = str(self._cfg("provider", "openai")).lower()
        fallback = "grok-imagine-image-2.0" if provider == "grok" else ("gpt-image-1" if provider == "openai" else "claude-3-5-sonnet-latest")
        try:
            models = await self._list_models()
            hints = ("image", "grok") if provider == "grok" else (("image", "dall", "gpt-image") if provider == "openai" else ("image", "claude"))
            for model in models:
                if any(hint in model.lower() for hint in hints):
                    return model
            if models:
                return models[0]
        except Exception as exc:
            logger.warning(f"[{_PLUGIN_NAME}] 获取模型列表失败，使用默认模型: {exc}")
        return fallback

    def _write_temp(self, data: bytes, suffix: str = ".png") -> str:
        fd, path = tempfile.mkstemp(prefix="astrbot-ai-image-", suffix=suffix)
        os.close(fd)
        Path(path).write_bytes(data)
        self._temp_paths.add(path)
        return path

    @staticmethod
    def _data_url_suffix(header: str) -> str:
        lowered = header.lower()
        if "jpeg" in lowered or "jpg" in lowered:
            return ".jpg"
        if "webp" in lowered:
            return ".webp"
        if "gif" in lowered:
            return ".gif"
        return ".png"

    async def _download(self, source: str) -> str:
        if source.startswith("file://"):
            return url2pathname(unquote(urlparse(source).path))
        if os.path.isfile(source):
            return source
        if source.startswith("base64://"):
            return self._write_temp(base64.b64decode(source[len("base64://"):]), ".png")
        if source.startswith("data:image/") and ";base64," in source:
            header, encoded = source.split(",", 1)
            return self._write_temp(base64.b64decode(encoded), self._data_url_suffix(header))
        if not source.startswith(("http://", "https://")):
            raise RuntimeError(f"无法识别的图片地址：{source[:80]}")
        # Grok may return an image URL whose CDN takes longer than the API
        # request itself to become available. Give result downloads their own
        # budget instead of inheriting the short request timeout.
        response = await (await self._http()).get(
            source,
            timeout=httpx.Timeout(120.0, connect=15.0),
        )
        response.raise_for_status()
        return self._write_temp(
            response.content,
            self._data_url_suffix(response.headers.get("content-type", "")),
        )

    @staticmethod
    def _ratio_size(ratio: str) -> str:
        sizes = {
            "1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536",
            "4:3": "1536x1152", "3:4": "1152x1536", "16:9": "1536x864", "9:16": "864x1536",
        }
        return sizes[ratio]

    def _prompt_ratio(self, prompt: str) -> str | None:
        match = re.search(r"\b(1:1|3:2|2:3|4:3|3:4|16:9|9:16)\b", prompt)
        if match:
            return match.group(1)
        if re.search(r"竖版|竖屏|手机壁纸|人像海报", prompt):
            return "9:16"
        if re.search(r"横版|横屏|电脑壁纸|宽屏|电影画幅", prompt):
            return "16:9"
        return None

    async def _resolve_size(
        self,
        prompt: str,
        image_path: str | None = None,
        setting_key: str = "generate_size",
    ) -> str:
        # Keep the old `size` field as a fallback for existing configurations.
        configured = str(
            self._cfg(setting_key, self._cfg("size", "auto")) or "auto"
        ).strip().lower()
        if configured in {"1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16"}:
            return self._ratio_size(configured)
        if configured not in {"", "auto"}:
            return configured
        if image_path:
            try:
                with PILImage.open(image_path) as image:
                    width, height = image.size
                ratios = {"1:1": 1, "3:2": 1.5, "2:3": 2 / 3, "4:3": 4 / 3, "3:4": 3 / 4, "16:9": 16 / 9, "9:16": 9 / 16}
                ratio = width / height
                nearest = min(ratios, key=lambda key: abs(ratios[key] - ratio))
                return self._ratio_size(nearest)
            except Exception as exc:
                logger.warning(f"[{_PLUGIN_NAME}] 无法读取原图尺寸，使用提示词/默认尺寸: {exc}")
        ratio = self._prompt_ratio(prompt)
        return self._ratio_size(ratio) if ratio else "1024x1024"

    async def _openai(self, prompt: str, image: str | None) -> str:
        client = await self._http()
        headers = self._headers()
        provider = str(self._cfg("provider", "openai")).lower()
        if image:
            path = await self._download(image)
            if provider == "grok":
                raw = Path(path).read_bytes()
                media = self._image_media_type(path)
                image_data = f"data:{media};base64,{base64.b64encode(raw).decode()}"
                response = await client.post(
                    f"{self._base()}/images/generations",
                    headers=headers,
                    json={
                        "model": await self._model(),
                        "prompt": prompt,
                        "image": [image_data],
                        "n": 1,
                        "response_format": "b64_json",
                    },
                )
                logger.info(f"[{_PLUGIN_NAME}] Grok 图片 API 响应 HTTP {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                return await self._extract_openai_image(payload)
            size = await self._resolve_size(prompt, path, "edit_size")
            with open(path, "rb") as handle:
                response = await client.post(
                    f"{self._base()}/images/edits",
                    headers={"authorization": headers["authorization"]},
                    data={"prompt": prompt, "model": await self._model(), "size": size},
                    files={"image": (Path(path).name, handle, "application/octet-stream")},
                )
        else:
            payload = {"model": await self._model(), "prompt": prompt, "size": await self._resolve_size(prompt, setting_key="generate_size"), "n": 1}
            if provider == "grok":
                payload["response_format"] = "b64_json"
            response = await client.post(f"{self._base()}/images/generations", headers=headers, json=payload)
        logger.info(f"[{_PLUGIN_NAME}] 图片 API 响应 HTTP {response.status_code}")
        response.raise_for_status()
        payload = response.json()
        return await self._extract_openai_image(payload)

    async def _extract_openai_image(self, payload: Any) -> str:
        rejection = self._rejection_from_payload(payload)
        if rejection is not None:
            raise ImageRequestRejected(rejection)
        items = payload.get("data") or payload.get("images") or []
        item = items[0] if isinstance(items, list) and items else payload
        image_url = item.get("url") or item.get("image_url") or item.get("imageUrl")
        if image_url:
            return await self._download(image_url)
        encoded = item.get("b64_json") or item.get("b64") or item.get("base64")
        if encoded:
            return self._write_temp(base64.b64decode(encoded), ".png")
        raise RuntimeError("API 未返回图片")

    @staticmethod
    def _image_media_type(path: str) -> str:
        suffix = Path(path).suffix.lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }.get(suffix, "image/png")

    async def _claude(self, prompt: str, image: str | None) -> str:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if image:
            path = await self._download(image)
            raw = Path(path).read_bytes()
            media = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
            content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.b64encode(raw).decode()}})
        endpoint = f"{self._base()}/messages" if self._base().endswith("/v1") else f"{self._base()}/v1/messages"
        response = await (await self._http()).post(endpoint, headers=self._headers(), json={"model": await self._model(), "max_tokens": 4096, "messages": [{"role": "user", "content": content}]})
        response.raise_for_status()
        payload = response.json()
        for block in payload.get("content", []):
            if block.get("type") == "image":
                source = block.get("source", {})
                if source.get("data"):
                    return self._write_temp(base64.b64decode(source["data"]), ".png")
                if source.get("url"):
                    return await self._download(source["url"])
        raise RuntimeError("Claude 原生 API 不提供生图；请配置支持图片输出的 Anthropic 兼容网关，或切换 provider=openai")

    async def _run(self, prompt: str, image: str | None = None) -> str:
        if str(self._cfg("provider", "openai")).lower() == "claude":
            return await self._claude(prompt, image)
        return await self._openai(prompt, image)

    def _ordered_profiles(self) -> list[str | None]:
        profiles = self._profiles()
        names = sorted(
            profiles,
            key=lambda name: (
                int(profiles[name].get("priority", 0))
                if str(profiles[name].get("priority", 0)).lstrip("-").isdigit()
                else 0
            ),
        )
        active = str(self.config.get("default_provider", "") or "").strip()
        # Native AstrBot template_list has no dynamic select control for a
        # separate default name. The first provider is therefore the default.
        ordered: list[str | None] = ([active] if active in names else names[:1])
        ordered.extend(name for name in names if name not in ordered)
        if not ordered:
            ordered.append(None)
        return ordered

    async def _run_with_failover(self, prompt: str, image: str | None = None) -> str:
        """默认供应商优先，失败后重试并切换其他供应商。"""
        try:
            limit = max(1, int(self.config.get("max_concurrent_requests", 1) or 1))
        except (TypeError, ValueError):
            limit = 1
        if self._request_slots is None or self._request_slots_count != limit:
            self._request_slots = asyncio.Semaphore(limit)
            self._request_slots_count = limit
        async with self._request_slots:
            errors: list[str] = []
            for profile in self._ordered_profiles():
                retries = 1
                if profile:
                    value = self._profiles().get(profile, {}).get("retry_count", 1)
                    try:
                        retries = max(1, int(value))
                    except (TypeError, ValueError):
                        retries = 1
                for attempt in range(1, retries + 1):
                    token = self._request_profile.set(profile)
                    self._models_cache = None
                    try:
                        logger.info(f"[{_PLUGIN_NAME}] 使用配置 {profile or 'legacy'}，第 {attempt}/{retries} 次尝试")
                        result = await self._run_with_timeout(prompt, image)
                        return result
                    except Exception as exc:
                        rejection = self._rejection_message(exc)
                        if rejection is not None:
                            raise ImageRequestRejected(rejection) from exc
                        errors.append(f"{profile or 'legacy'}: {self._error_detail(exc)}")
                        logger.warning(f"[{_PLUGIN_NAME}] 配置 {profile or 'legacy'} 尝试失败: {exc}")
                    finally:
                        self._request_profile.reset(token)
            raise RuntimeError("\n".join(errors[-5:]) or "所有生图供应商均不可用")

    async def _send_error_reply(self, event: AstrMessageEvent, message: str) -> None:
        result = event.plain_result(message)
        message_obj = getattr(event, "message_obj", None)
        message_id = getattr(message_obj, "id", None) or getattr(message_obj, "message_id", None)
        chain = getattr(result, "chain", None)
        if message_id is not None and isinstance(chain, list):
            chain.insert(0, Reply(id=message_id))
        await event.send(result)

    async def _run_with_timeout(self, prompt: str, image: str | None = None) -> str:
        timeout = max(10.0, float(self._cfg("timeout_sec", 60) or 60))
        if str(self._cfg("provider", "openai")).lower() == "grok":
            timeout = max(timeout, 180.0)
        return await asyncio.wait_for(self._run(prompt, image), timeout=timeout)

    @staticmethod
    def _message_components(event: AstrMessageEvent) -> list[Any]:
        """展开当前消息与引用消息的组件链，兼容各平台适配器。"""
        message_obj = getattr(event, "message_obj", None)
        components: list[Any] = []
        for chain in (
            getattr(message_obj, "message", None),
            getattr(event, "message_chain", None),
        ):
            if isinstance(chain, (list, tuple)):
                components.extend(chain)
        for component in components.copy():
            if isinstance(component, Reply):
                quoted = getattr(component, "chain", None)
                if isinstance(quoted, (list, tuple)):
                    components.extend(quoted)
        return components

    @staticmethod
    def _platform_name(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name() or "")
        except Exception:
            return str(getattr(getattr(event, "platform_meta", None), "name", "") or "")

    @staticmethod
    def _is_direct_image_source(source: str) -> bool:
        return source.startswith(
            ("http://", "https://", "file://", "base64://", "data:image/")
        ) or os.path.isfile(source)

    def _avatar_url(self, event: AstrMessageEvent, user_id: str) -> str | None:
        """按平台构造用户头像地址。QQ 官方机器人使用 appid + openid。"""
        user_id = str(user_id or "").strip()
        if not user_id:
            return None
        if self._platform_name(event) in {"qq_official", "qq_official_webhook"}:
            bot = getattr(event, "bot", None) or getattr(event, "_bot", None)
            appid = str(
                getattr(getattr(bot, "platform", None), "appid", "") or ""
            ).strip()
            if not appid:
                return None
            return (
                f"https://q.qlogo.cn/qqapp/{quote(appid, safe='')}/"
                f"{quote(user_id, safe='')}/100"
            )
        if user_id.isdigit():
            return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        return None

    @staticmethod
    def _attachment_urls(container: Any) -> list[str]:
        """从 attachments 列表中提取图片 URL（兼容 dict / 对象字段）。"""
        data = _as_dict(container)
        if not isinstance(data, dict):
            return []
        attachments = data.get("attachments")
        if not isinstance(attachments, list):
            return []
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        urls: list[str] = []
        for attachment in attachments:
            info = _as_dict(attachment)
            if not isinstance(info, dict):
                continue
            content_type = str(
                info.get("content_type") or info.get("contentType") or ""
            ).lower()
            filename = str(info.get("filename") or info.get("name") or "")
            url = str(info.get("url") or "").strip()
            if not url:
                continue
            if content_type.startswith("image") or Path(filename).suffix.lower() in image_exts:
                if not url.startswith(("http://", "https://")):
                    url = f"https://{url}"
                urls.append(url)
        return urls

    @staticmethod
    def _raw_image_urls(event: AstrMessageEvent) -> list[str]:
        """兜底：从原始消息的 msg_elements / attachments 读取图片。

        部分 AstrBot 版本的 QQ 官方适配器不会把被引用消息的附件写进
        Reply.chain，此时消息链里没有 Image 组件，只能读原始 payload。
        """
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        data = _as_dict(raw)
        if not isinstance(data, dict):
            return []
        containers: list[Any] = [data]
        raw_data = data.get("raw_data")
        if isinstance(raw_data, dict):
            containers.append(raw_data)
            payload = raw_data.get("d")
            if isinstance(payload, dict):
                containers.append(payload)
        urls: list[str] = []
        for container in containers:
            info = _as_dict(container)
            if not isinstance(info, dict):
                continue
            urls.extend(AiImagePlugin._attachment_urls(info))
            for element in info.get("msg_elements") or []:
                urls.extend(AiImagePlugin._attachment_urls(element))
        return list(dict.fromkeys(urls))

    async def _event_images(self, event: AstrMessageEvent) -> list[str]:
        message_obj = getattr(event, "message_obj", None)
        urls: list[str] = []
        # Prefer AstrBot components: adapters normalize platform-specific
        # payloads into Image components (url / local path / base64 / file id).
        for component in self._message_components(event):
            if not isinstance(component, AstrImage):
                continue
            source = str(
                component.url or component.file or component.path or ""
            ).strip()
            if source and self._is_direct_image_source(source):
                urls.append(source)
                continue
            converter = getattr(component, "convert_to_file_path", None)
            if callable(converter):
                try:
                    urls.append(await converter())
                except Exception as exc:
                    logger.warning(f"[{_PLUGIN_NAME}] 无法读取消息图片: {exc}")
        urls.extend(self._raw_image_urls(event))
        values = [
            getattr(message_obj, "message", None),
            getattr(event, "message_chain", None),
            getattr(event, "message", None),
            getattr(event, "raw_message", None),
            message_obj,
        ]
        urls.extend(url for value in values for url in _find_image_urls(value))
        urls = list(dict.fromkeys(urls))
        if not urls:
            components = self._message_components(event)
            component_names = [type(component).__name__ for component in components]
            reply_chains = [
                [type(item).__name__ for item in (getattr(component, "chain", None) or [])]
                for component in components
                if isinstance(component, Reply)
            ]
            raw = getattr(message_obj, "raw_message", None)
            raw_data = _as_dict(raw)
            raw_keys = sorted(raw_data.keys()) if isinstance(raw_data, dict) else []
            logger.info(
                f"[{_PLUGIN_NAME}] 消息中未找到图片：组件={component_names}，"
                f"引用链={reply_chains}，raw_type={type(raw).__name__}，raw_keys={raw_keys}"
            )
        return urls

    def _event_at_ids(self, event: AstrMessageEvent) -> list[str]:
        message_obj = getattr(event, "message_obj", None)
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = str(getattr(message_obj, "self_id", "") or "")
        ids: list[str] = []
        for component in self._message_components(event):
            if isinstance(component, At):
                qq = str(
                    getattr(component, "qq", "")
                    or getattr(component, "user_id", "")
                    or ""
                ).strip()
                if qq and qq not in _AT_SENTINELS and qq != self_id:
                    ids.append(qq)
        values = [
            getattr(message_obj, "message", None),
            getattr(event, "message_chain", None),
            message_obj,
            getattr(event, "raw_message", None),
        ]
        ids.extend(qq for value in values for qq in _find_at_ids(value))
        # CQ at segments are sometimes retained only in raw_message/message_str.
        for raw in (getattr(event, "message_str", ""), getattr(event, "raw_message", "")):
            ids += re.findall(r"\[CQ:at,qq=(\d+)\]", str(raw or ""), re.I)
        return [
            qq
            for qq in dict.fromkeys(ids)
            if qq not in _AT_SENTINELS and qq != self_id
        ]

    @staticmethod
    def _command_match(pattern: re.Pattern[str], message: str):
        """匹配媒体占位前缀后的命令，如“[图片] 改图 ...”。"""
        return pattern.search((message or "").strip())

    @filter.regex(_MODELS_RE, priority=21)
    async def models(self, event: AstrMessageEvent):
        """列出当前 API 可用模型，便于填写 model 配置。"""
        event.stop_event()
        try:
            models = await self._list_models(refresh=True)
            if not models:
                yield event.plain_result("API 未返回模型列表，请手动填写 model。")
                return
            yield event.plain_result("当前可用模型：\n" + "\n".join(f"- {model}" for model in models[:100]))
        except Exception as exc:
            yield event.plain_result(f"获取模型失败：{exc}")

    @filter.regex(_GEN_RE, priority=20)
    async def generate(self, event: AstrMessageEvent):
        event.stop_event()
        raw = event.message_str or ""
        match = self._command_match(_GEN_RE, raw)
        prompt = match.group(1).strip() if match else ""
        logger.info(f"[{_PLUGIN_NAME}] 命中生图命令，描述长度={len(prompt)}")
        if not prompt:
            yield event.plain_result("用法：生图 <描述>，例如：生图 生成一副随机油画")
            return
        progress = str(self._cfg("generate_progress", "已收到生图请求，正在生成图片，请稍候…") or "").strip()
        if progress:
            await self._send_progress(event, progress)
        try:
            logger.info(f"[{_PLUGIN_NAME}] 开始请求生图服务器，模型配置={self._cfg('model', 'auto')}")
            yield event.image_result(await self._run_with_failover(prompt))
        except Exception as exc:
            logger.error(f"[{_PLUGIN_NAME}] 生图失败: {exc}")
            await self._send_error_reply(event, self._api_error(exc))

    @filter.regex(_EDIT_RE, priority=20)
    async def edit(self, event: AstrMessageEvent):
        event.stop_event()
        raw = event.message_str or ""
        match = self._command_match(_EDIT_RE, raw)
        prompt = match.group(1).strip() if match else ""
        images = await self._event_images(event)
        at_ids = self._event_at_ids(event)
        if not images and at_ids:
            avatar = self._avatar_url(event, at_ids[0])
            if avatar:
                images = [avatar]
                logger.info(
                    f"[{_PLUGIN_NAME}] 未找到图片，使用被艾特用户头像作为原图，"
                    f"qq={at_ids[0]}，platform={self._platform_name(event)}"
                )
            else:
                logger.info(
                    f"[{_PLUGIN_NAME}] 当前平台无法构造被艾特用户头像，"
                    f"qq={at_ids[0]}，platform={self._platform_name(event)}"
                )
        logger.info(f"[{_PLUGIN_NAME}] 命中改图命令，描述长度={len(prompt)}，识别图片数={len(images)}，艾特数={len(at_ids)}，消息={raw[:120]!r}")
        if not images:
            if at_ids:
                yield event.plain_result("当前平台无法自动获取被艾特用户的头像，请附带或引用一张图片后重试。")
            else:
                yield event.plain_result("请附带或引用一张图片；也可以艾特一位 QQ 用户后发送「改图 <描述>」，插件会使用其头像。")
            return
        if not prompt:
            yield event.plain_result("用法：改图 <描述>，例如：改图 把脸部优化得自然一些")
            return
        progress = str(self._cfg("edit_progress", "已收到改图请求，正在处理图片，请稍候…") or "").strip()
        if progress:
            await self._send_progress(event, progress)
        try:
            logger.info(f"[{_PLUGIN_NAME}] 开始请求改图服务器，模型配置={self._cfg('model', 'auto')}")
            yield event.image_result(await self._run_with_failover(prompt, images[0]))
        except Exception as exc:
            logger.error(f"[{_PLUGIN_NAME}] 改图失败: {exc}")
            await self._send_error_reply(event, self._api_error(exc))

    async def terminate(self):
        if self._client:
            await self._client.aclose()
            self._client = None
        for path in self._temp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._temp_paths.clear()
