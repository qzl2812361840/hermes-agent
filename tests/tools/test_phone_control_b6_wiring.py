"""B6 接线测试：phone_control tap/tap_text/type_text/swipe 点后核验（HERMES_ACTION_VERIFY 门控，默认 OFF 零影响）。"""
import json

import pytest

from tools import phone_control as pc


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


@pytest.fixture()
def _probe(monkeypatch):
    monkeypatch.setattr(pc.httpx, "get", lambda *a, **k: _Resp({"any_online": True}))
    monkeypatch.setattr("agent.post_action_verifier.time.sleep", lambda s: None)


def test_gate_off_unchanged(monkeypatch, _probe):
    """门控默认 OFF：tap 行为与接线前一致，不读树、无 verify 字段。"""
    monkeypatch.delenv("HERMES_ACTION_VERIFY", raising=False)
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"]["action"])
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="tap", params={"x": 100, "y": 200}))
    assert calls == ["tap"]
    assert "verify" not in out
    assert out["ok"] is True


def test_verify_success_path(monkeypatch, _probe):
    """ON：动作前读树存哈希 → 动作 → 树变了 → VERIFIED，动作只执行一次。"""
    monkeypatch.setenv("HERMES_ACTION_VERIFY", "ON")
    calls = []
    trees = iter(["tree-A", "tree-B"])  # 执行前 A，执行后 B

    def fake_post(url, **kw):
        a = kw["json"]["action"]
        calls.append(a)
        if a == "ui_tree":
            return _Resp({"ok": True, "status": "done", "output": next(trees)})
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="tap", params={"x": 1, "y": 2}))
    assert calls == ["ui_tree", "tap", "ui_tree"]
    assert out["ok"] is True
    v = out["verify"]
    assert v["status"] == "VERIFIED"
    assert v["attempts"] == 1
    assert v["checks"][0]["probe_ok"] is True


def test_retry_then_verified(monkeypatch, _probe):
    """ON：首轮树没变 → 重试一次后变化 → RETRY_VERIFIED，共执行两次动作。"""
    monkeypatch.setenv("HERMES_ACTION_VERIFY", "ON")
    calls = []
    trees = iter(["tree-A", "tree-A", "tree-B"])  # 前=A；首轮探测=A（没变）→ 重试后=B

    def fake_post(url, **kw):
        a = kw["json"]["action"]
        calls.append(a)
        if a == "ui_tree":
            return _Resp({"ok": True, "status": "done", "output": next(trees)})
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="type_text", params={"text": "hi"}))
    assert calls.count("type_text") == 2
    v = out["verify"]
    assert v["status"] == "RETRY_VERIFIED"
    assert v["attempts"] == 2
    assert [c["probe_ok"] for c in v["checks"]] == [False, True]


def test_failed_after_retries(monkeypatch, _probe):
    """ON：树始终没变 → 阶梯耗尽（首轮+2 重试）→ FAILED，动作共执行三次。"""
    monkeypatch.setenv("HERMES_ACTION_VERIFY", "ON")
    calls = []

    def fake_post(url, **kw):
        a = kw["json"]["action"]
        calls.append(a)
        if a == "ui_tree":
            return _Resp({"ok": True, "status": "done", "output": "tree-A"})
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="swipe", params={"x1": 1, "y1": 2, "x2": 3, "y2": 4}))
    assert calls.count("swipe") == 3
    v = out["verify"]
    assert v["status"] == "FAILED"
    assert v["attempts"] == 3
    assert all(not c["probe_ok"] for c in v["checks"])


def test_skip_when_tree_unreadable_before(monkeypatch, _probe):
    """ON：动作前读不到元素树 = 无可靠读回口子 → 不硬套核验，行为照旧。"""
    monkeypatch.setenv("HERMES_ACTION_VERIFY", "ON")
    calls = []

    def fake_post(url, **kw):
        a = kw["json"]["action"]
        calls.append(a)
        if a == "ui_tree":
            return _Resp({"ok": False, "status": "error", "error": "device offline"})
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="tap_text", params={"target": "确定"}))
    assert calls == ["ui_tree", "tap_text"]  # 只读了一次树，动作照常执行
    assert "verify" not in out
    assert out["ok"] is True


def test_probe_unreadable_midway_counts_not_met(monkeypatch, _probe):
    """ON：核验中途读树失败按未达成处理（不瞎判成功），走完阶梯如实上报。"""
    monkeypatch.setenv("HERMES_ACTION_VERIFY", "ON")
    calls = []
    first = {"done": False}

    def fake_post(url, **kw):
        a = kw["json"]["action"]
        calls.append(a)
        if a == "ui_tree":
            if not first["done"]:
                first["done"] = True
                return _Resp({"ok": True, "status": "done", "output": "tree-A"})
            return _Resp({"ok": False, "status": "error", "error": "screen off"})
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="tap", params={"x": 1, "y": 2}))
    assert calls.count("tap") == 3
    v = out["verify"]
    assert v["status"] == "FAILED"
    assert all(c["probe_ok"] is False for c in v["checks"])
