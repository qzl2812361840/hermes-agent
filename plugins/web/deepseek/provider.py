"""DeepSeek (https://api.deepseek.com) web search + page fetch — keyed provider.

Env: ``DEEPSEEK_API_KEY`` (https://platform.deepseek.com/api_keys).
APIs: ``POST /v1/tools/search`` (web-wide search, billed per successful call) and
``POST /v1/tools/fetch`` (URL → title + Markdown content). Both are standalone
tools on the DeepSeek OpenAPI surface — independent of any model call.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from plugins.web._common import (
    SEARCH_LIMIT_CAP, BaseWebSearchProvider, document, http_status_detail, page_error, provider_env,
    run_extract, run_search, search_fail, search_ok, setup_schema, web_hit,
)

logger = logging.getLogger(__name__)

_DEEPSEEK_TOOLS_URL = "https://api.deepseek.com/v1/tools"
_TIMEOUT = 30


def _deepseek_headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}


def _require_key() -> str:
    api_key = provider_env("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is not set (https://platform.deepseek.com/api_keys)")
    return api_key


class DeepseekWebSearchProvider(BaseWebSearchProvider):
    """DeepSeek Search + Fetch provider (keyed)."""

    NAME = "deepseek"
    DISPLAY_NAME = "DeepSeek"
    KEY_ENV = "DEEPSEEK_API_KEY"
    EXTRACT = True
    KEYLESS = False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        def _body() -> Dict[str, Any]:
            import requests
            api_key = _require_key()
            logger.info("DeepSeek search: '%s' (limit=%d)", query, limit)
            response = requests.post(
                f"{_DEEPSEEK_TOOLS_URL}/search",
                json={"query": query, "count": min(max(1, int(limit)), SEARCH_LIMIT_CAP)},
                headers=_deepseek_headers(api_key), timeout=_TIMEOUT,
            )
            if response.status_code >= 400:
                return search_fail(f"DeepSeek search failed: {http_status_detail(response)}")
            # ``summary`` (600-800 chars) is DeepSeek's model-facing digest; ``snippet`` is the raw excerpt.
            return search_ok([
                web_hit(r.get("url") or "", r.get("title") or "", r.get("summary") or r.get("snippet") or "", i + 1)
                for i, r in enumerate(response.json().get("results") or [])
            ])

        return run_search("DeepSeek", logger, _body, verbatim_value_error=False)

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        def _body() -> List[Dict[str, Any]]:
            import requests
            api_key = _require_key()
            logger.info("DeepSeek extract: %d URL(s)", len(urls))
            results: List[Dict[str, Any]] = []
            for url in urls:
                try:
                    response = requests.post(
                        f"{_DEEPSEEK_TOOLS_URL}/fetch", json={"url": url},
                        headers=_deepseek_headers(api_key), timeout=_TIMEOUT,
                    )
                    if response.status_code >= 400:
                        raise ValueError(http_status_detail(response))
                    data = response.json()
                    results.append(document(
                        data.get("url") or url, data.get("title") or "", data.get("content") or "", source_url=url,
                    ))
                except Exception as exc:  # noqa: BLE001 — per-URL error entry
                    results.append(page_error(url, f"DeepSeek extract failed: {exc}"))
            return results

        return run_extract("DeepSeek", logger, urls, _body, verbatim_value_error=False)

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "DeepSeek", "paid",
            "DeepSeek Search + Fetch: web-wide search (foreign sites included) and URL → Markdown page "
            "content, billed per successful call. Failed calls fall back to the keyless free-tier ring.",
            "DEEPSEEK_API_KEY", "DeepSeek API key", "https://platform.deepseek.com/api_keys",
        )
