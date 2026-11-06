"""queue_effects 单元测试：合并去重、指数退避、TransientLater、健康阈值、保序、30天清理。"""
import sqlite3

import pytest

from agent.queue_effects import (
    FAILURES_BEFORE_ERROR,
    INITIAL_DELAY_MS,
    MAX_DELAY_MS,
    EffectsQueue,
    TransientLater,
    UnknownKind,
)


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _queue(db, handlers=None, t=[0]):
    t[0] = 1_000_000
    return EffectsQueue(db, handlers, now_ms=lambda: t[0])


class Clock:
    def __init__(self):
        self.ms = 1_000_000

    def __call__(self):
        return self.ms

    def advance(self, delta):
        self.ms += delta


def test_enqueue_and_dispatch_success(db):
    got = []
    clock = Clock()
    q = EffectsQueue(db, {"mail": lambda p: got.append(p)}, now_ms=clock)
    q.enqueue("mail", "m1", {"to": "a"})
    assert q.pending("mail") == 1
    assert q.dispatch("mail") == 1
    assert got == [{"to": "a"}]
    assert q.pending() == 0


def test_same_kind_key_merges(db):
    clock = Clock()
    q = EffectsQueue(db, {}, now_ms=clock)
    q.enqueue("mail", "m1", {"v": 1})
    q.enqueue("mail", "m1", {"v": 2})
    assert q.pending("mail") == 1
    row = db.execute("SELECT value, generation FROM effect").fetchone()
    assert row["value"] == '{"v": 2}' and row["generation"] == 1


def test_retry_with_exponential_backoff(db):
    clock = Clock()
    calls = []
    q = EffectsQueue(db, {}, now_ms=clock)

    def boom(_):
        calls.append(1)
        raise RuntimeError("down")

    q.register("mail", boom)
    q.enqueue("mail", "m1", {"v": 1})
    q.dispatch("mail")
    row = db.execute("SELECT attempts, error, due FROM effect").fetchone()
    assert row["attempts"] == 1
    assert row["due"] == clock.ms + INITIAL_DELAY_MS
    assert "down" in row["error"]
    # 第 3 次失败才标健康错误
    for _ in range(FAILURES_BEFORE_ERROR - 1):
        clock.ms += MAX_DELAY_MS
        q.dispatch("mail")
    assert "mail" in q.health()
    # 退避封顶
    assert clock.ms >= 1_000_000


def test_transient_later_no_attempt_increase(db):
    clock = Clock()
    q = EffectsQueue(db, {}, now_ms=clock)

    def busy(_):
        raise TransientLater()

    q.register("mail", busy)
    q.enqueue("mail", "m1", {"v": 1})
    q.dispatch("mail")
    row = db.execute("SELECT attempts, due FROM effect").fetchone()
    assert row["attempts"] == 0
    assert row["due"] == clock.ms + INITIAL_DELAY_MS


def test_unknown_kind_keeps_row_without_fail_count(db):
    clock = Clock()
    q = EffectsQueue(db, {}, now_ms=clock)
    q.enqueue("ghost", "g1", {"v": 1})
    with pytest.raises(UnknownKind):
        q.dispatch("ghost")
    row = db.execute("SELECT attempts FROM effect").fetchone()
    assert row["attempts"] == 0
    assert q.pending("ghost") == 1


def test_ordered_seq_and_block(db):
    clock = Clock()
    q = EffectsQueue(db, {}, now_ms=clock)
    q.enqueue("mail", "first", {"n": 1})
    # second 入队后 first 失败进入退避 → dispatch 会先撞上 first
    def flaky(payload):
        if payload["n"] == 1:
            raise RuntimeError("x")
        return None

    q.register("mail", flaky)
    q.enqueue("mail", "second", {"n": 2})
    q.dispatch("mail")  # 只处理 first（失败）
    q.dispatch("mail")  # first 退避未到，本轮无事
    got = db.execute("SELECT key FROM effect WHERE due<=? ORDER BY seq", (clock.ms,)).fetchall()
    assert [g["key"] for g in got] == []


def test_health_error_cleared_view(db):
    clock = Clock()
    q = EffectsQueue(db, {}, now_ms=clock)
    assert q.health() == {}


def test_cleanup_done_respects_retention(db):
    clock = Clock()
    q = EffectsQueue(db, {"mail": lambda p: None}, now_ms=clock)
    q.enqueue("mail", "old", {"v": 1})
    q.dispatch("mail")
    # 前进 31 天
    clock.ms += 31 * 24 * 3600 * 1000
    assert q.cleanup_done() == 1
    assert q.cleanup_done() == 0
