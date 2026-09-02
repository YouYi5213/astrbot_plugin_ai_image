from __future__ import annotations

import base64
import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx
from PIL import Image as PILImage

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


_PLUGIN_NAME = "astrbot_plugin_ai_image"
_MEDIA_PREFIX = r"(?:\[[^\]]+\]\s*)*"
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
    if (typ.endswith(".at") or typ in {"at", "componenttype.at"} or class_name == "at") and str(data.get("qq", "")) != "all":
        qq = str(data.get("qq", "")).strip()
        if qq:
            result.append(qq)
    for key, child in data.items():
        if key not in {"qq", "name"}:
            result.extend(_find_at_ids(child, seen))
    return list(dict.fromkeys(result))


class AiImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._client: httpx.AsyncClient | None = None
        self._temp_paths: set[str] = set()
        self._models_cache: list[str] | None = None
        self._request_profile: str | None = None
        self._request_lock = asyncio.Lock()

    def _cfg(self, key: str, default: Any = "") -> Any:
        profiles = self._profiles()
        active = str(self.config.get("default_provider", "") or "").strip()
        if self._request_profile and self._request_profile in profiles:
            return profiles[self._request_profile].get(key, self.config.get(key, default))
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
    def _api_error(exc: Exception) -> str:
        if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
            return "生图服务器请求超时，请稍后重试。"
        if isinstance(exc, httpx.ConnectError):
            return "生图服务器连接失败，请检查 API 地址或服务状态。"
        if isinstance(exc, httpx.HTTPStatusError):
            detail = exc.response.text[:300].replace("\n", " ")
            return f"生图服务器异常（HTTP {exc.response.status_code}）：{detail}"
        if isinstance(exc, httpx.HTTPError):
            return f"生图服务器异常：{exc}"
        return f"生图失败：{exc}"

    def _headers(self) -> dict[str, str]:
        key = str(self._cfg("api_key", "") or "").strip()
        if not key:
            raise RuntimeError("未配置 API Key，请在插件配置中填写")
        if str(self._cfg("provider", "openai")).lower() == "claude":
            return {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        return {"authorization": f"Bearer {key}", "content-type": "application/json"}

    def _base(self) -> str:
        return str(self._cfg("api_base", "https://api.openai.com/v1") or "").rstrip("/")

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
        fallback = "gpt-image-1" if provider == "openai" else "claude-3-5-sonnet-latest"
        try:
            models = await self._list_models()
            hints = ("image", "dall", "gpt-image") if provider == "openai" else ("image", "claude")
            for model in models:
                if any(hint in model.lower() for hint in hints):
                    return model
            if models:
                return models[0]
        except Exception as exc:
            logger.warning(f"[{_PLUGIN_NAME}] 获取模型列表失败，使用默认模型: {exc}")
        return fallback

    async def _download(self, source: str) -> str:
        if source.startswith("file://"):
            return source[7:]
        if os.path.isfile(source):
            return source
        response = await (await self._http()).get(source)
        response.raise_for_status()
        suffix = ".png"
        content_type = response.headers.get("content-type", "")
        if "jpeg" in content_type or "jpg" in content_type:
            suffix = ".jpg"
        fd, path = tempfile.mkstemp(prefix="astrbot-ai-image-", suffix=suffix)
        os.close(fd)
        Path(path).write_bytes(response.content)
        self._temp_paths.add(path)
        return path

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
        if image:
            path = await self._download(image)
            size = await self._resolve_size(prompt, path, "edit_size")
            with open(path, "rb") as handle:
                response = await client.post(
                    f"{self._base()}/images/edits",
                    headers={"authorization": headers["authorization"]},
                    data={"prompt": prompt, "model": await self._model(), "size": size},
                    files={"image": (Path(path).name, handle, "application/octet-stream")},
                )
        else:
            response = await client.post(f"{self._base()}/images/generations", headers=headers, json={"model": await self._model(), "prompt": prompt, "size": await self._resolve_size(prompt, setting_key="generate_size"), "n": 1})
        logger.info(f"[{_PLUGIN_NAME}] 图片 API 响应 HTTP {response.status_code}")
        response.raise_for_status()
        payload = response.json()
        item = (payload.get("data") or [{}])[0]
        if item.get("url"):
            return await self._download(item["url"])
        if item.get("b64_json"):
            fd, path = tempfile.mkstemp(prefix="astrbot-ai-image-", suffix=".png")
            os.close(fd)
            Path(path).write_bytes(base64.b64decode(item["b64_json"]))
            self._temp_paths.add(path)
            return path
        raise RuntimeError("API 未返回图片")

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
                    fd, path = tempfile.mkstemp(prefix="astrbot-ai-image-", suffix=".png")
                    os.close(fd); Path(path).write_bytes(base64.b64decode(source["data"])); self._temp_paths.add(path); return path
                if source.get("url"):
                    return await self._download(source["url"])
        raise RuntimeError("Claude 原生 API 不提供生图；请配置支持图片输出的 Anthropic 兼容网关，或切换 provider=openai")

    async def _run(self, prompt: str, image: str | None = None) -> str:
        if str(self._cfg("provider", "openai")).lower() == "claude":
            return await self._claude(prompt, image)
        return await self._openai(prompt, image)

    def _ordered_profiles(self) -> list[str | None]:
        names = self._profile_names()
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
        async with self._request_lock:
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
                    self._request_profile = profile
                    self._models_cache = None
                    try:
                        logger.info(f"[{_PLUGIN_NAME}] 使用配置 {profile or 'legacy'}，第 {attempt}/{retries} 次尝试")
                        result = await self._run_with_timeout(prompt, image)
                        self._request_profile = None
                        return result
                    except Exception as exc:
                        errors.append(f"{profile or 'legacy'}: {self._api_error(exc)}")
                        logger.warning(f"[{_PLUGIN_NAME}] 配置 {profile or 'legacy'} 尝试失败: {exc}")
            self._request_profile = None
            raise RuntimeError("；".join(errors[-5:]) or "所有生图供应商均不可用")

    async def _run_with_timeout(self, prompt: str, image: str | None = None) -> str:
        timeout = max(10.0, float(self._cfg("timeout_sec", 60) or 60))
        return await asyncio.wait_for(self._run(prompt, image), timeout=timeout)

    def _event_images(self, event: AstrMessageEvent) -> list[str]:
        message_obj = getattr(event, "message_obj", None)
        values = [
            getattr(message_obj, "message", None),
            getattr(event, "message_chain", None),
            getattr(event, "message", None),
            getattr(event, "raw_message", None),
            message_obj,
        ]
        urls = [url for value in values for url in _find_image_urls(value)]
        # Some adapters expose the quoted chain separately from message_obj.message.
        for component in getattr(message_obj, "message", None) or []:
            if type(component).__name__.lower() == "reply":
                urls.extend(_find_image_urls(getattr(component, "chain", None)))
        return list(dict.fromkeys(urls))

    def _event_at_ids(self, event: AstrMessageEvent) -> list[str]:
        message_obj = getattr(event, "message_obj", None)
        values = [getattr(message_obj, "message", None), getattr(event, "message_chain", None), message_obj, getattr(event, "raw_message", None)]
        ids = [qq for value in values for qq in _find_at_ids(value)]
        # CQ at segments are sometimes retained only in raw_message/message_str.
        for raw in (getattr(event, "message_str", ""), getattr(event, "raw_message", "")):
            for qq in re.findall(r"\[CQ:at,qq=(\d+)\]", str(raw or ""), re.I):
                ids.append(qq)
        return list(dict.fromkeys(ids))

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
            yield event.plain_result(self._api_error(exc))

    @filter.regex(_EDIT_RE, priority=20)
    async def edit(self, event: AstrMessageEvent):
        event.stop_event()
        raw = event.message_str or ""
        match = self._command_match(_EDIT_RE, raw)
        prompt = match.group(1).strip() if match else ""
        images = self._event_images(event)
        at_ids = self._event_at_ids(event)
        if not images and at_ids:
            # QQ public avatar endpoint; use the first explicitly mentioned user.
            images = [f"https://q1.qlogo.cn/g?b=qq&nk={at_ids[0]}&s=640"]
            logger.info(f"[{_PLUGIN_NAME}] 未找到图片，使用被艾特用户头像作为原图，qq={at_ids[0]}")
        logger.info(f"[{_PLUGIN_NAME}] 命中改图命令，描述长度={len(prompt)}，识别图片数={len(images)}，艾特数={len(at_ids)}，消息={raw[:120]!r}")
        if not images:
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
            yield event.plain_result(self._api_error(exc))

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
