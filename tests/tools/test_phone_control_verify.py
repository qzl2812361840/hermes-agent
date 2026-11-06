"""B2 接线测试：phone_control set_wifi/set_bluetooth 读回核验（HERMES_MUTATION_VERIFY 门控，默认 OFF 零影响）。"""
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


def test_gate_off_unchanged(monkeypatch, _probe):
    """门控默认 OFF：set_* 行为与接线前一致，无读回、无 verify 字段。"""
    monkeypatch.delenv("HERMES_MUTATION_VERIFY", raising=False)
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"]["action"])
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="set_wifi", params={"on": True}))
    assert calls == ["set_wifi"]
    assert "verify" not in out
    assert out["ok"] is True


def test_verify_success_path(monkeypatch, _probe):
    """ON：read→mutate→read，开关生效且期望一致 → verified=True。"""
    monkeypatch.setenv("HERMES_MUTATION_VERIFY", "ON")
    actions = []
    reads = {"n": 0}

    def fake_post(url, **kw):
        a = kw["json"]["action"]
        actions.append(a)
        if a == "shell":
            reads["n"] += 1
            val = "0" if reads["n"] == 1 else "1"  # 执行前关，执行后开
            return _Resp({"ok": True, "status": "done", "output": val})
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="set_wifi", params={"on": True}))
    assert actions == ["shell", "set_wifi", "shell"]
    assert out["ok"] is True
    v = out["verify"]
    assert v["status"] == "SUCCEEDED"
    assert v["before"] == "0" and v["after"] == "1" and v["expected"] == "1"
    assert v["changed"] is True and v["verified"] is True


def test_refused_when_state_unreadable(monkeypatch, _probe):
    """ON：读不到当前状态 → 拒绝执行（fail-closed），写操作不发生。"""
    monkeypatch.setenv("HERMES_MUTATION_VERIFY", "ON")
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"]["action"])
        if kw["json"]["action"] == "shell":
            return _Resp({"ok": False, "status": "error", "error": "device offline"})
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="set_bluetooth", params={"on": False}))
    assert calls == ["shell"]  # 只读了状态，没执行写
    assert out["ok"] is False
    assert out["verify"]["status"] == "REFUSED"
    assert out["verify"]["failure_code"].startswith("STATE_UNAVAILABLE")


def test_verify_mismatch_reported_not_lied(monkeypatch, _probe):
    """ON：执行成功但读回无变化 → 如实标注 verified=False，不谎报成功。"""
    monkeypatch.setenv("HERMES_MUTATION_VERIFY", "ON")
    reads = {"n": 0}

    def fake_post(url, **kw):
        if kw["json"]["action"] == "shell":
            reads["n"] += 1
            return _Resp({"ok": True, "status": "done", "output": "1"})  # 前后都是开
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="set_wifi", params={"on": True}))
    assert out["ok"] is True  # 执行成功
    v = out["verify"]
    assert v["status"] == "SUCCEEDED"
    assert v["changed"] is False and v["verified"] is False


def test_pending_reported_as_timed_out(monkeypatch, _probe):
    """ON：命令入队未回报（pending）→ TIMED_OUT，不当作失败。"""
    monkeypatch.setenv("HERMES_MUTATION_VERIFY", "ON")

    def fake_post(url, **kw):
        if kw["json"]["action"] == "shell":
            return _Resp({"ok": True, "status": "done", "output": "0"})
        return _Resp({"ok": False, "status": "pending", "output": "", "id": "c1"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="set_wifi", params={"on": True}))
    assert out["status"] == "pending"
    assert out["verify"]["status"] == "TIMED_OUT"


def test_flashlight_no_read_path_no_verify(monkeypatch, _probe):
    """set_flashlight 无可靠读回口子：即使门控 ON 也不套核验，行为不变。"""
    monkeypatch.setenv("HERMES_MUTATION_VERIFY", "ON")
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["json"]["action"])
        return _Resp({"ok": True, "status": "done", "output": "ok"})

    monkeypatch.setattr(pc.httpx, "post", fake_post)
    out = json.loads(pc.phone_control(action="set_flashlight", params={"on": True}))
    assert calls == ["set_flashlight"]
    assert "verify" not in out
    assert out["ok"] is True
