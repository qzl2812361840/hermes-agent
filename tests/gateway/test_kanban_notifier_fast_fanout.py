"""Tests for the C6 subscription fast-path fanout (学 dsh intake.ts fanout).

已订阅且 TTL 内确认过的看板直达认领链（免逐板只读探测 ``count_notify_subs``）；
未登记的看板走常规探测路径。跑法：
cd /d/父体/hermes-agent && .venv/bin/python -m pytest tests/gateway/test_kanban_notifier_fast_fanout.py -q
"""

import asyncio

from unittest.mock import patch

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    # 与既有 notifier 测试一致：持锁网关 + 默认 profile，legacy 订阅行才可见可投
    runner._kanban_dispatcher_lock_handle = object()
    runner._active_profile_name = lambda: "default"
    return runner


async def _run_notifier_ticks(monkeypatch, runner, ticks=1):
    """跑满 ticks 跳看板通知循环（同一次 watcher 调用内，与真实循环一致）。"""
    real_sleep = asyncio.sleep
    counter = {"n": 0}

    async def fake_sleep(delay):
        if delay == 5:
            return None
        counter["n"] += 1
        if counter["n"] >= ticks:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _create_completed_task():
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="fast fanout", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
        return tid
    finally:
        conn.close()


def test_subscribed_board_goes_direct_after_first_tick(tmp_path, monkeypatch):
    """第二跳起直通：免 count_notify_subs 只读探测，事件照常认领投递。"""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "fast.db"))
    kb.init_db()
    _create_completed_task()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    with patch.object(kbn, "count_notify_subs", wraps=kbn.count_notify_subs) as spy_probe:
        async def two_ticks():
            _create_completed_task()  # 首跳后造新完成事件，验证第二跳直通链照常认领投递
            await _run_notifier_ticks(monkeypatch, runner, ticks=2)

        asyncio.run(two_ticks())  # ticks=2：第一跳常规探测+投递，第二跳直通
        assert len(adapter.sent) == 2  # 两跳各投递一次
        assert spy_probe.call_count == 1  # 第二跳未再走只读探测


def test_unsubscribed_board_stays_on_regular_path(tmp_path, monkeypatch):
    """无订阅看板不进直通登记：每跳仍走常规探测路径且零投递。"""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "zero.db"))
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="zero subs", assignee="worker")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    with patch.object(kbn, "count_notify_subs", wraps=kbn.count_notify_subs) as spy_probe:
        asyncio.run(_run_notifier_ticks(monkeypatch, runner, ticks=2))
    assert adapter.sent == []
    assert not runner._kanban_fast_seen
    assert spy_probe.call_count == 2  # 无登记 → 每跳常规探测


def test_registry_pruned_when_subs_removed(tmp_path, monkeypatch):
    """订阅清空后登记被摘除，回到常规路径（行为收敛到真实订阅状态）。"""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "prune.db"))
    kb.init_db()
    _create_completed_task()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_notifier_ticks(monkeypatch, runner, ticks=1))
    assert runner._kanban_fast_seen

    # 清空订阅 → 下一跳摘登记
    conn = kbc.connect()
    try:
        kbn.remove_notify_sub(conn, task_id=_subs_task_id(),
                              platform="telegram", chat_id="chat-1")
    finally:
        conn.close()
    runner._running = True  # 上一跳已置停；恢复运行态让第二次调用真正跑到循环
    asyncio.run(_run_notifier_ticks(monkeypatch, runner, ticks=1))
    assert not runner._kanban_fast_seen


def _subs_task_id():
    """取当前唯一的订阅任务 id（测试辅助）。"""
    conn = kbc.connect()
    try:
        subs = kbn.list_notify_subs(conn)
        return subs[0]["task_id"] if subs else ""
    finally:
        conn.close()
