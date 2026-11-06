"""task_queue 单测：合并、退避、健康、并发、保序、清理、重放、恢复。"""

import time

import pytest

from agent.task_queue import (
    FAILURES_BEFORE_ERROR,
    RETENTION_DAYS,
    QueueConfig,
    TaskQueue,
    TaskStatus,
)


@pytest.fixture
def q(tmp_path):
    queue = TaskQueue(str(tmp_path / "q.db"))
    yield queue
    queue.close()


def test_enqueue_and_due_roundtrip(q):
    seen = []

    def handler(key, payload):
        seen.append((key, payload))

    q.register("reply", handler)
    assert q.enqueue("reply", "m1", {"text": "hi"}) is True
    rows = q.due("reply")
    assert len(rows) == 1
    key, payload, gen = rows[0]
    assert key == "m1" and payload == {"text": "hi"}
    assert q.claim("reply", key, gen) is True
    q.complete("reply", key)
    assert q.due("reply") == []
    assert q.stats("reply") == {"pending": 0, "running": 0, "failed": 0}


def test_coalesce_same_kind_key(q):
    q.register("reply", lambda k, p: None)
    assert q.enqueue("reply", "m1", {"v": 1}) is True
    assert q.enqueue("reply", "m1", {"v": 2}) is False  # 合并
    rows = q.due("reply")
    assert len(rows) == 1 and rows[0][1] == {"v": 2}  # 保留新载荷


def test_generation_blocks_stale_claim(q):
    q.register("reply", lambda k, p: None)
    q.enqueue("reply", "m1", {"v": 1})
    _, _, gen1 = q.due("reply")[0]
    q.enqueue("reply", "m1", {"v": 2})  # 合并 generation+1
    assert q.claim("reply", "m1", gen1) is False  # 旧代认领无效
    _, _, gen2 = q.due("reply")[0]
    assert q.claim("reply", "m1", gen2) is True


def test_exponential_backoff(q):
    q.register("reply", lambda k, p: None, limit=2)
    q.enqueue("reply", "m1")
    key, _, gen = q.due("reply")[0]
    q.claim("reply", key, gen)
    q.fail("reply", key)
    assert q.due("reply") == []  # 退避中未到点
    row = q._db.execute("SELECT attempts, due FROM task_queue WHERE key='m1'").fetchone()
    assert row[0] == 1
    assert row[1] > time.time()  # due 在未来


def test_fail_three_marks_error_but_not_blocked(q):
    q.register("reply", lambda k, p: None, limit=2)
    q2 = TaskQueue(":memory:", QueueConfig(max_attempts_before_failed=8, retry_base=0.0))
    q2.register("reply", lambda k, p: None)
    q2.enqueue("reply", "x")
    for _ in range(FAILURES_BEFORE_ERROR):
        rows = q2.due("reply")
        if rows:
            key, _, gen = rows[0]
            q2.claim("reply", key, gen)
            q2.fail("reply", key)
        else:
            # retry_base=0 → delay=0，due 到点即可再取
            pass
    assert q2.health("reply").status == "error"  # 连续 3 次失败
    assert q2.stats("reply")["pending"] == 1  # 不阻塞（仍是 pending 可再取）


def test_max_attempts_terminal_failed_and_requeue(q):
    q2 = TaskQueue(":memory:", QueueConfig(max_attempts_before_failed=3, retry_base=0.0))
    q2.register("reply", lambda k, p: None)
    q2.enqueue("reply", "x")
    for _ in range(3):
        rows = q2.due("reply")
        if rows:
            key, _, gen = rows[0]
            q2.claim("reply", key, gen)
            q2.fail("reply", key)
        else:
            q2._db.execute("UPDATE task_queue SET due=0 WHERE key='x'")
            q2._db.commit()
    assert q2.stats("reply")["failed"] == 1  # 终态
    assert q2.due("reply") == []  # 终态不再自动执行
    assert len(q2.failed_rows("reply")) == 1
    assert q2.requeue_failed("reply", "x") is True  # 重放入口
    assert q2.due("reply") != []


def test_ordered_kind_is_serial(q):
    q.register("step", lambda k, p: None, ordered=True)
    q.enqueue("step", "s1")
    q.enqueue("step", "s2")
    rows = q.due("step")
    assert len(rows) == 1 and rows[0][0] == "s1"
    key, _, gen = rows[0]
    q.claim("step", key, gen)
    assert q.due("step") == []  # 前一单没完结，后面不发光


def test_purge_expired_failed_rows(q):
    q.register("reply", lambda k, p: None)
    q.enqueue("reply", "old")
    rows = q.due("reply")
    key, _, gen = rows[0]
    q.claim("reply", key, gen)
    # 造终态
    q._db.execute("UPDATE task_queue SET status='failed', updated_at=0 WHERE key=?", (key,))
    q._db.commit()
    assert q.purge_expired() == 1  # 30 天前的失败行清掉
    assert q.stats("reply")["failed"] == 0
    assert q.stats("reply")["pending"] == 0


def test_persistence_across_reopen(tmp_path):
    path = str(tmp_path / "p.db")
    q1 = TaskQueue(path)
    q1.register("reply", lambda k, p: None)
    q1.enqueue("reply", "m1", {"v": 1})
    q1.close()
    q2 = TaskQueue(path)
    q2.register("reply", lambda k, p: None)  # 重新注册 handler
    rows = q2.due("reply")
    assert len(rows) == 1 and rows[0][1] == {"v": 1}  # 重启不丢
    q2.close()


def test_unregistered_kind_due_empty(q):
    q.enqueue("ghost", "g1")
    assert q.due("ghost") == []
