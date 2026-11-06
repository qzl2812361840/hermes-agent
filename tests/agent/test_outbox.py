"""outbox 单元测试：保序、重试上限、留痕不删、幂等 ack。"""
import sqlite3

from agent.outbox import MAX_ATTEMPTS, Outbox


def _db():
    return sqlite3.connect(":memory:")


def test_fifo_ordered_delivery():
    sent = []
    ob = Outbox(_db(), lambda ch, p: sent.append((ch, p)))
    a = ob.push("wx", "m1")
    b = ob.push("wx", "m2")
    assert ob.flush("wx") == 2
    assert sent == [("wx", "m1"), ("wx", "m2")]
    assert ob.is_acked(a) and ob.is_acked(b)


def test_failure_blocks_following():
    sent = []
    state = {"fail": True}

    def sender(ch, p):
        if state["fail"]:
            raise RuntimeError("down")
        sent.append(p)

    ob = Outbox(_db(), sender)
    ob.push("wx", "m1")
    ob.push("wx", "m2")
    assert ob.flush("wx") == 0  # m1 失败，m2 不出
    state["fail"] = False
    assert ob.flush("wx") == 2  # m1 通了 m2 才走


def test_drop_after_max_attempts_keeps_trace():
    state = {"fail": True}

    def sender(ch, p):
        raise RuntimeError("dead")

    ob = Outbox(_db(), sender)
    ob.push("wx", "m1")
    for _ in range(MAX_ATTEMPTS):
        ob.flush("wx")
    assert ob.pending("wx") == 0  # 从待发队列移除
    assert len(ob.dropped) == 1  # 但留痕
    d = ob.dropped[0]
    assert d.reason == "dead" and d.at_ms > 0


def test_ack_idempotent():
    ob = Outbox(_db(), lambda ch, p: None)
    msg_id = ob.push("wx", "m1")
    ob.flush("wx")
    assert ob.is_acked(msg_id) is True
    # 重复 ack 不报错不变化
    assert ob.is_acked(msg_id) is True
