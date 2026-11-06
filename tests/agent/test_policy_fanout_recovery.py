"""policy_version / monitor_fanout / recovery 单元测试。"""
from agent.monitor_fanout import Subscriptions, fanout
from agent.policy_version import Mandate, needs_revalidate, policy_changed_notifications, policy_version
from agent.recovery import PersistedTicket, recover

import pytest


# ── policy_version ────────────────────────────────────────
def test_version_stable_and_order_insensitive():
    v1 = policy_version({"a": 1, "b": [2, 3]})
    v2 = policy_version({"b": [2, 3], "a": 1})
    assert v1 == v2 and len(v1) == 16


def test_version_changes_with_content():
    assert policy_version({"a": 1}) != policy_version({"a": 2})


def test_revalidate_and_notifications():
    v_old = policy_version({"x": 1})
    v_new = policy_version({"x": 2})
    m1 = Mandate("t1", v_old)
    m2 = Mandate("t2", v_new)
    assert needs_revalidate(m1, v_new) is True
    assert needs_revalidate(m2, v_new) is False
    notes = policy_changed_notifications([m1, m2], v_new)
    assert [n["ticket_id"] for n in notes] == ["t1"]
    assert notes[0]["type"] == "policy-changed"


# ── monitor_fanout ────────────────────────────────────────
def test_subscribed_passthrough_and_idempotent():
    subs = Subscriptions()
    got = []

    def h(content):
        got.append(content)

    assert subs.subscribe("股价", h) is True
    assert subs.subscribe("股价", h) is False  # 幂等
    assert fanout(subs, "股价", "新内容") is True
    assert got == ["新内容"]


def test_unsubscribed_falls_back():
    subs = Subscriptions()
    assert fanout(subs, "天气", "x") is False


# ── recovery ──────────────────────────────────────────────
def test_recovery_oldest_first_and_actions():
    tickets = [
        PersistedTicket("t3", created_at=300, running=True),
        PersistedTicket("t1", created_at=100, pending=True),
        PersistedTicket("t2", created_at=200, waiting_person=True),
        PersistedTicket("t0", created_at=50, ended="done"),
    ]
    out = recover(tickets)
    assert [r.ticket_id for r in out] == ["t1", "t2", "t3"]
    assert out[0].action == "relaunch"  # pending 且不等人 → 重新入队
    assert out[1].action == "keep_waiting"  # 等人的不动
    assert out[2].action == "resume" and "中断" in out[2].note
    assert all("t0" != r.ticket_id for r in out)  # 已结束的跳过
