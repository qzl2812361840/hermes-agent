"""Hermes 内容审核开关接入(2026-10-22 主人令)。

语义:
- hermes_filter_enabled = false(默认) → 输出原样, 无审核(现状)
- hermes_filter_enabled = true  → 复用嘎妞后端统一词库做输出检查
  (block → 占位文本; mask → 打码; warn/pass → 原样)

约束:
- 后端词库/规则/数据文件零改动(只读加载 content_filter 模块)
- 后端模块加载失败 → 原样放行, 绝不阻断业务
"""
from __future__ import annotations

import importlib.util
import os
from typing import Any, Optional

_HERMES_HOME = os.environ.get("HERMES_HOME", "/d/父体/hermes-home")
# 嘎妞后端统一词库过滤器(同一台机器, 只读复用)
_BACKEND_CF_PATH = "/d/嘎妞后端/app/services/content_filter.py"

_cf_module: Optional[Any] = None


def _filter_enabled() -> bool:
    """读取 agent.hermes_filter_enabled 配置; 读不到一律视为停用。"""
    try:
        import yaml
        with open(os.path.join(_HERMES_HOME, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return bool((cfg.get("agent") or {}).get("hermes_filter_enabled", False))
    except Exception:
        return False


def _backend_check(text: str):
    """懒加载后端 content_filter.check_text; 失败返回 None(调用方放行)。"""
    global _cf_module
    try:
        if _cf_module is None:
            spec = importlib.util.spec_from_file_location("_hermes_backend_cf", _BACKEND_CF_PATH)
            if spec is None or spec.loader is None:
                return None
            _cf_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_cf_module)
        return _cf_module.check_text(text)
    except Exception:
        return None


def filter_assistant_text(text: str) -> str:
    """对 assistant 输出文本做审核; 开关停用或加载失败时原样返回。"""
    if not text:
        return text
    if not _filter_enabled():
        return text
    result = _backend_check(text)
    if result is None:
        return text
    action, cleaned, _hits = result
    if action == "block":
        return "[内容不合规已过滤]"
    return cleaned or text
