"""DeepSeek web search + extract plugin — bundled, auto-loaded; keyed."""
from __future__ import annotations
# 相对导入：插件既在 repo  bundled 目录（plugins/web/deepseek），也会经
# ~/.hermes/plugins（用户目录/共享挂载）被发现——两种位置都用
# spec_from_file_location 加载为 hermes_plugins.<slug>，绝对导入
# ``plugins.web.deepseek`` 只在 repo 根在 sys.path 时成立，用户目录位置会
# ModuleNotFoundError。相对导入两种位置都能解析。
from .provider import DeepseekWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(DeepseekWebSearchProvider())
