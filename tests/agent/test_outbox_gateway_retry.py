"""C4 保序回信接线测试：留痕、按序补投、失败升级留痕、flush_async 与同步 flush 同语义。"""
import asyncio

from agent.outbox import MAX_ATTEMPTS, Outbox
from gateway.outbox_retry import OutboxRetry


def _retry(tmp_path):
    return OutboxRetry(tmp_path / "outbox_retry.db")


def _decode(payload: str) -> dict:
    import json
    return json.loads(payload)


def test_record_failure_then_flush_once(tmp_path):
    """留痕后补投按原内容重发，成功后清账。"""
    retry = _retry(tmp_path)
    retry.record_failure("telegram", "123", "hello", {"job_id": "j1"})
    assert retry.outbox.pending() == 1
    sent = []

    async def asend(channel, payload):
        sent.append((channel, _decode(payload)))

    n = asyncio.run(retry.flush_once(asend))
    assert n == 1 and sent[0][0] == "telegram:123"
    assert sent[0][1]["content"] == "hello" and sent[0][1]["metadata"] == {"job_id": "j1"}
    assert retry.outbox.pending() == 0
    retry.close()


def test_ordering_blocks_following_channel_wise(tmp_path):
    """同通道前一条未通则后一条不出；通道之间互不挡。"""
    retry = _retry(tmp_path)
    retry.record_failure("telegram", "123", "m1")
    retry.record_failure("telegram", "123", "m2")
    retry.record_failure("wework", "456", "w1")
    state = {"fail": True}
    sent = []

    async def asend(channel, payload):
        if state["fail"] and channel.startswith("telegram"):
            raise RuntimeError("down")
        sent.append((channel, _decode(payload)["content"]))

    n = asyncio.run(retry.flush_once(asend))
    assert n == 1 and ("wework:456", "w1") in sent  # telegram 失败，独立通道 w1 正常发出
    assert retry.outbox.pending("telegram:123") == 2
    state["fail"] = False
    assert asyncio.run(retry.flush_once(asend)) == 2  # m1 通了 m2 才走
    assert [c for c, _ in sent[-2:]] == ["telegram:123", "telegram:123"]
    assert [p for _, p in sent[-2:]] == ["m1", "m2"]
    retry.close()


def test_escalation_after_max_attempts_keeps_trace(tmp_path):
    """连续失败达上限：从待发移除、留痕进 dropped，不静默。"""
    retry = _retry(tmp_path)
    retry.record_failure("telegram", "123", "m1")

    async def asend(channel, payload):
        raise RuntimeError("dead")

    for _ in range(MAX_ATTEMPTS):
        asyncio.run(retry.flush_once(asend))
    assert retry.outbox.pending() == 0
    assert len(retry.outbox.dropped) == 1 and retry.outbox.dropped[0].reason == "dead"
    # 留痕表里也在（重启后可查）
    rows = retry.db.execute("SELECT channel, reason FROM outbox_dropped").fetchall()
    assert rows == [("telegram:123", "dead")]
    retry.close()


def test_flush_async_same_semantics_as_sync_flush():
    """flush_async 与 flush 语义一致：保序、成功标 done、失败退避。"""
    sent = []
    ob = Outbox(sqlite3_memory(), lambda ch, p: None)  # sync sender 不用，走 flush_async

    async def asend(channel, payload):
        sent.append((channel, payload))

    ob.push("wx", "a1")
    ob.push("wx", "a2")
    assert asyncio.run(ob.flush_async("wx", asend)) == 2
    assert sent == [("wx", "a1"), ("wx", "a2")]


def sqlite3_memory():
    import sqlite3
    return sqlite3.connect(":memory:")
