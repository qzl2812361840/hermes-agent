# -*- coding: utf-8 -*-
"""C6 订阅直通接线补丁：看板事件分发入口挂 monitor_fanout 快路径（学 dsh intake.ts fanout）。

TTL 内确认过订阅的看板直达认领投递链（免逐板只读探测）；未登记的走常规探测路径。
跑法：cd /d/父体/hermes-agent && .venv/bin/python scripts/patch_c6_fanout.py
"""
import io

PATH = "gateway/kanban_watchers_notifier.py"
with io.open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

CHANGES = []

# ── 1) 顶部导入 ──
old = '''import asyncio
import contextlib
import re
from functools import partial
from pathlib import Path
import weakref
from typing import Any, Callable, Optional

from agent.i18n import t
'''
new = '''import asyncio
import contextlib
import re
import time
from functools import partial
from pathlib import Path
import weakref
from typing import Any, Callable, Optional

from agent.i18n import t
from agent.monitor_fanout import Subscriptions, fanout
'''
assert src.count(old) == 1, "anchor-1 not unique"
src = src.replace(old, new)
CHANGES.append("imports")

# ── 2) TTL 常量（挂在 TERMINAL_KINDS 附近）──
old = '''TERMINAL_KINDS = ("completed",'''
new = '''# C6 订阅直通登记有效期：TTL 内的看板直达认领链路（免逐板只读探测），过期回到常规探测。
# 订阅变化最迟 TTL 秒后收敛到真实状态（collect_board 每跳回填/摘除）。
_FAST_SUB_TTL_SECONDS = 300.0

TERMINAL_KINDS = ("completed",'''
assert src.count(old) == 1, "anchor-2 not unique"
src = src.replace(old, new)
CHANGES.append("ttl constant")

# ── 3) _Collector：本跳直通表构建 ──
old = '''        self.include_unowned = runner._owns_kanban_dispatcher_lock()
        self.profile_adapters = getattr(runner, "_profile_adapters", {})
'''
new = '''        self.include_unowned = runner._owns_kanban_dispatcher_lock()
        self.profile_adapters = getattr(runner, "_profile_adapters", {})
        # C6 订阅直通（学 dsh intake.ts fanout 到 thread.subscriptions）：已登记订阅的看板
        # 跳过逐板只读探测直达认领链路；未登记的走常规探测（探测命中后回填登记）
        self.fast_seen: dict[str, float] = getattr(runner, "_kanban_fast_seen", None) or {}
        runner._kanban_fast_seen = self.fast_seen
        self.fast_subs = Subscriptions()
        now_ts = time.time()
        for seen_slug, seen_at in list(self.fast_seen.items()):
            if now_ts - seen_at <= _FAST_SUB_TTL_SECONDS:
                self.fast_subs.subscribe(seen_slug, self._fast_board_chain(seen_slug))
            else:
                self.fast_seen.pop(seen_slug, None)
'''
assert src.count(old) == 1, "anchor-3 not unique"
src = src.replace(old, new)
CHANGES.append("collector fast registry")

# ── 4) collect()：fanout 直通门 ──
old = '''            if resolved_db_path in seen_db_paths:
                logger.debug("kanban notifier: skipping duplicate board slug %s for DB %s", slug, resolved_db_path)
                continue
            seen_db_paths.add(resolved_db_path)
            self.collect_board(slug)
        return self.deliveries
'''
new = '''            if resolved_db_path in seen_db_paths:
                logger.debug("kanban notifier: skipping duplicate board slug %s for DB %s", slug, resolved_db_path)
                continue
            seen_db_paths.add(resolved_db_path)
            # C6：已订阅 → fanout 直达该看板认领链（返回 True）；未订阅 → 常规探测路径
            if not fanout(self.fast_subs, slug, ""):
                self.collect_board(slug)
        return self.deliveries
'''
assert src.count(old) == 1, "anchor-4 not unique"
src = src.replace(old, new)
CHANGES.append("collect fanout gate")

# ── 5) collect_board：skip_probe + 登记回填/摘除 ──
old = '''    def collect_board(self, slug: str) -> None:
        """Claim events on one board, appending delivery dicts to ``deliveries``."""
        if not self._board_has_subs(slug):
            return
'''
new = '''    def collect_board(self, slug: str, *, skip_probe: bool = False) -> None:
        """Claim events on one board, appending delivery dicts to ``deliveries``."""
        # C6：skip_probe=True 时来自直通登记（TTL 内确认过订阅），免只读探测直开连接
        if not skip_probe and not self._board_has_subs(slug):
            return
'''
assert src.count(old) == 1, "anchor-5 not unique"
src = src.replace(old, new)
CHANGES.append("collect_board skip_probe")

old = '''            subs = _kbn().list_notify_subs(conn, notifier_profiles=self.notifier_profiles, include_unowned=self.include_unowned)
            if not subs:
                logger.debug("kanban notifier: board %s has no subscriptions", slug)
'''
new = '''            subs = _kbn().list_notify_subs(conn, notifier_profiles=self.notifier_profiles, include_unowned=self.include_unowned)
            if subs:
                # C6：确认订阅在 → 回填直通登记（幂等），下跳起 fanout 直达
                self.fast_seen[slug] = time.time()
            else:
                # C6：订阅清空 → 摘登记，下跳恢复常规探测
                self.fast_seen.pop(slug, None)
            if not subs:
                logger.debug("kanban notifier: board %s has no subscriptions", slug)
'''
assert src.count(old) == 1, "anchor-6 not unique"
src = src.replace(old, new)
CHANGES.append("registry backfill/prune")

# ── 6) 直通链处理器 ──
old = '''    def _board_has_subs(self, slug: str) -> bool:'''
new = '''    def _fast_board_chain(self, slug: str) -> Callable[[str], None]:
        """C6 直通链：fanout 命中后直达该看板的认领投递（与常规路径同一套逻辑，免探测）。"""
        def _chain(_content: str) -> None:
            self.collect_board(slug, skip_probe=True)
        return _chain

    def _board_has_subs(self, slug: str) -> bool:'''
assert src.count(old) == 1, "anchor-7 not unique"
src = src.replace(old, new)
CHANGES.append("fast board chain")

with io.open(PATH, "w", encoding="utf-8") as f:
    f.write(src)
print("OK:", "; ".join(CHANGES))
