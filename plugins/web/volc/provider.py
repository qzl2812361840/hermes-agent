"""火山方舟（豆包）联网搜索 — Responses API web_search 工具。

Config: ``web.search_backend: volc``。
Env: ``VOLCANO_ARK_KEY``（方舟 API Key；缺省时回退已存在的 ``VOLCANO_KB_API_KEY``）。
Endpoint: ``https://ark.cn-beijing.volces.com/api/v3/responses``
（``VOLCANO_ARK_ENDPOINT`` 可覆盖；``VOLCANO_SEARCH_MODEL`` 可换驱动模型）。

未开通「联网内容插件」时方舟返回 ToolNotOpen —— 本 provider 自动回落已注册的
DeepSeek provider（``DEEPSEEK_API_KEY``）并在日志留痕；控制台开通后无需改任何
配置，下一次搜索即走火山（组件关闭状态有 5 分钟负缓存，开通后最多等 5 分钟）。
开通地址: https://console.volcengine.com/common-buy/CC_content_plugin

只有 search 能力：方舟没有页面抓取 API，``web.extract_backend`` 继续用 deepseek。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from plugins.web._common import (
    SEARCH_LIMIT_CAP, BaseWebSearchProvider, http_status_detail, provider_env,
    run_search, search_fail, search_ok, setup_schema, web_hit,
)

logger = logging.getLogger(__name__)

_RESPONSES_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
_DEFAULT_MODEL = "doubao-seed-2-1-pro-260915"
_TIMEOUT = 60
_COMPONENT_URL = "https://console.volcengine.com/common-buy/CC_content_plugin"
# 组件未开通的负缓存 TTL：避免每次搜索都先白跑一次方舟；开通后最多这么久恢复火山链路
_CLOSED_TTL_SECONDS = 300

_closed_lock = threading.Lock()
_closed_until = 0.0


def _resolve_key() -> str:
    """方舟 Key：VOLCANO_ARK_KEY 优先，其次复用知识库场景的 VOLCANO_KB_API_KEY。"""
    for env_name in ("VOLCANO_ARK_KEY", "VOLCANO_KB_API_KEY"):
        key = provider_env(env_name)
        if key:
            return key
    return ""


def _endpoint() -> str:
    return (provider_env("VOLCANO_ARK_ENDPOINT") or _RESPONSES_URL).rstrip("/")


def _model() -> str:
    return provider_env("VOLCANO_SEARCH_MODEL") or _DEFAULT_MODEL


def _component_closed_cached() -> bool:
    with _closed_lock:
        return time.monotonic() < _closed_until


def _mark_component_closed() -> None:
    global _closed_until
    with _closed_lock:
        _closed_until = time.monotonic() + _CLOSED_TTL_SECONDS


def _is_component_not_open(status: int, body: str) -> bool:
    """方舟对未购买的组件返回 404 ToolNotOpen（web_search）或 403 AccessDenied（image_process）。"""
    if status not in (402, 403, 404):
        return False
    low = (body or "").lower()
    return "toolnotopen" in low or "not activated" in low or "accessdenied" in low


def _iter_output_texts(data: Dict[str, Any]) -> Iterator[Tuple[str, List[Any]]]:
    """yield ``(text, annotations)`` for every ``output_text`` content part in ``output``."""
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("text"):
                yield str(part["text"]), part.get("annotations") or []


def _parse_hits(data: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    """Responses 输出 → web hits。

    - 每条 ``url_citation`` 注释成一条来源（description 取被引用的原文片段）；
    - 模型合成的答案作为第一条 hit（空 URL、标题标注「摘要」），保证被 limit 截断时仍在；
    - 顶层 ``output_text`` 作为无 output 条目时的兜底答案。
    """
    hits: List[Dict[str, Any]] = []
    answer_parts: List[str] = []
    seen_urls = set()
    for text, annotations in _iter_output_texts(data):
        answer_parts.append(text)
        for ann in annotations:
            if not isinstance(ann, dict):
                continue
            url = str(ann.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            span = ""
            start, end = ann.get("start_index"), ann.get("end_index")
            if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
                span = text[start:end].strip()
            hits.append(web_hit(url, str(ann.get("title") or "").strip(), span, len(hits) + 1))
    answer = "\n".join(p for p in answer_parts if p).strip() or str(data.get("output_text") or "").strip()
    if answer:
        hits.insert(0, web_hit("", "火山方舟·联网搜索摘要", answer, 1))
        for i, hit in enumerate(hits):
            hit["position"] = i + 1
    return hits[:limit]


def _fallback_to_deepseek(query: str, limit: int) -> Dict[str, Any]:
    """组件未开通时回落已注册的 DeepSeek provider（search_backend 原实现，现仍在用）。"""
    deepseek = None
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
        from agent.web_search_registry import get_provider
        deepseek = get_provider("deepseek")
    except Exception as exc:  # noqa: BLE001 — registry 不可用时给出可读失败
        logger.warning("volc 回落 DeepSeek 时 registry 不可用: %s", exc)
    if deepseek is not None and deepseek.supports_search():
        logger.info("volc 联网搜索回落 DeepSeek: '%s' (limit=%d)", query, limit)
        return deepseek.search(query, limit)
    return search_fail(
        "火山方舟「联网内容插件」未开通（" + _COMPONENT_URL + "），且 DeepSeek 回落不可用"
        "（DEEPSEEK_API_KEY 未配置？）"
    )


class VolcWebSearchProvider(BaseWebSearchProvider):
    """火山方舟（豆包）联网搜索 — keyed；组件未开通时自动回落 DeepSeek。"""

    NAME = "volc"
    DISPLAY_NAME = "火山方舟（豆包）联网搜索"
    KEY_ENV = "VOLCANO_ARK_KEY"
    EXTRACT = False
    KEYLESS = False

    def is_available(self) -> bool:
        return bool(_resolve_key())

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        def _body() -> Dict[str, Any]:
            import requests
            api_key = _resolve_key()
            if not api_key:
                raise ValueError(
                    "VOLCANO_ARK_KEY is not set（方舟 API Key："
                    "https://console.volcengine.com/ark/region:ark+cn-beijing/apikey）"
                )
            count = min(max(1, int(limit)), SEARCH_LIMIT_CAP)
            if _component_closed_cached():
                return _fallback_to_deepseek(query, count)
            logger.info("火山方舟联网搜索: '%s' (limit=%d, model=%s)", query, count, _model())
            response = requests.post(
                _endpoint(),
                json={
                    "model": _model(), "stream": False,
                    "tools": [{"type": "web_search"}],
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": query}]}],
                },
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=_TIMEOUT,
            )
            if response.status_code >= 400:
                detail = http_status_detail(response)
                if _is_component_not_open(response.status_code, detail):
                    _mark_component_closed()
                    logger.warning(
                        "火山方舟「联网内容插件」未开通，%ds 内搜索回落 DeepSeek；开通地址 %s",
                        _CLOSED_TTL_SECONDS, _COMPONENT_URL,
                    )
                    return _fallback_to_deepseek(query, count)
                raise ValueError(f"火山方舟联网搜索失败({response.status_code}): {detail[:300]}")
            hits = _parse_hits(response.json(), count)
            if not hits:
                raise ValueError("火山方舟联网搜索未返回可用结果")
            return search_ok(hits)

        return run_search("火山方舟", logger, _body, verbatim_value_error=False)

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "火山方舟（豆包）联网搜索", "paid",
            "方舟 Responses API web_search：答案式联网搜索（含引用来源）。未开通「联网内容插件」时"
            "自动回落 DeepSeek，开通后零配置切回火山。仅搜索；页面提取仍用 deepseek。",
            "VOLCANO_ARK_KEY", "方舟 API Key（或复用已有的 VOLCANO_KB_API_KEY）",
            "https://console.volcengine.com/ark/region:ark+cn-beijing/apikey",
        )
