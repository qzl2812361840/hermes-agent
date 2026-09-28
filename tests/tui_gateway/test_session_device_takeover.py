"""ganiu (2026-09-29): a real user submit takes the session over from another window / device.

Upstream treats per-session exclusivity as a correctness guarantee: a second live owner is
refused with ``SESSION_NOT_OWNED`` ("This chat is open in another Hermes window/terminal"),
with exactly one exception (a same-process, client-less sibling runtime). That dead-ended the
operator in his own chat: a stale ``tui`` lease from a previous client made every new submit
fail. Product policy for this install is that the chat follows whoever is typing, even when
the previous owner is a live foreign process (TUI on the server, a second window, the phone).
Synthesized turns must never do that, so takeover is armed only by the user-submit gate
(``server._request_active_session_takeover``) and gated on surface (tui/desktop) +
``active_sessions.takeover`` (default ON for that gate, OFF for every other caller).

These tests pin all three properties: the upstream refusal still holds for everything that is
not a user submit, the user submit seizes (also across a real process boundary), and the
policy switches turn the feature off.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from hermes_cli.active_sessions import (
    SESSION_NOT_OWNED,
    SESSION_TAKEOVER_ENV,
    active_session_lease_is_registered,
    active_session_registry_snapshot,
    resolve_session_takeover,
)
from tui_gateway import server

_LEASE_HOLDER = """
import os, time
from pathlib import Path
from hermes_cli.active_sessions import try_acquire_active_session

lease, message = try_acquire_active_session(
    session_id=os.environ["SESSION_ID"], surface="tui", config={})
assert lease is not None and message is None, message
Path(os.environ["READY_FILE"]).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 120
release = Path(os.environ["RELEASE_FILE"])
while not release.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("timed out waiting for release")
    time.sleep(0.02)
lease.release()
"""


def _record(live_id: str, source: str) -> dict:
    return {
        "source": source, "session_key": "device-chat", "history": [], "running": False,
        "last_active": time.time(), "created_at": time.time(), "profile_home": None,
    }


def _spawn_lease_holder(tmp_path: Path, session_id: str) -> tuple[subprocess.Popen[str], Path]:
    """A live FOREIGN process holding the session's lease (the other device / window)."""
    ready, release = tmp_path / "ready", tmp_path / "release"
    env = dict(os.environ, SESSION_ID=session_id, READY_FILE=str(ready), RELEASE_FILE=str(release))
    child = subprocess.Popen([sys.executable, "-c", _LEASE_HOLDER], env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 60
    while not ready.exists():
        if child.poll() is not None:
            raise AssertionError(f"lease holder died: {child.stderr.read() if child.stderr else ''}")
        if time.monotonic() >= deadline:
            raise AssertionError("lease holder never became ready")
        time.sleep(0.02)
    return child, release


def _stop_holder(child: subprocess.Popen[str], release: Path) -> None:
    release.write_text("go", encoding="utf-8")
    try:
        child.wait(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()


def _registry_owner() -> tuple[str, str]:
    """(live_session_id, surface) of the single registry entry — asserts exactly ONE entry."""
    (entry,) = active_session_registry_snapshot()
    metadata = entry.get("metadata") or {}
    return str(metadata.get("live_session_id") or ""), str(entry.get("surface") or "")


def test_live_foreign_lease_still_refuses_without_a_user_submit(tmp_path: Path, monkeypatch) -> None:
    """Upstream behaviour is untouched for anything that is not the user typing: a live foreign
    owner keeps refusing without the arm flag, even with the policy ON. This is what protects an
    open chat from auto-continue / heartbeat / bot turns."""
    monkeypatch.setenv(SESSION_TAKEOVER_ENV, "1")
    holder, release = _spawn_lease_holder(tmp_path, "device-chat")
    try:
        record = _record("second-runtime", source="tui")
        refusal = server._ensure_active_session_slot("second-runtime", record)
        assert getattr(refusal, "reason", None) == SESSION_NOT_OWNED
        assert record.get("active_session_lease") is None
        assert _registry_owner()[0] == ""
    finally:
        _stop_holder(holder, release)


def test_user_submit_seizes_the_session_from_another_live_surface(tmp_path: Path, monkeypatch) -> None:
    """The real submit path arms takeover, the claim replaces the foreign entry in place (leaving
    a record of what it took over from), and the arm is one-shot."""
    monkeypatch.setenv(SESSION_TAKEOVER_ENV, "1")
    holder, release = _spawn_lease_holder(tmp_path, "device-chat")
    try:
        foreign_pid = holder.pid
        record = _record("phone", source="tui")
        assert server._session_takeover_requested(record) is True
        assert server._request_active_session_takeover(record) is True

        assert server._ensure_active_session_slot("phone", record) is None
        assert record.get("active_session_lease") is not None
        assert "_active_session_takeover_armed" not in record

        assert _registry_owner() == ("phone", "tui")
        (entry,) = active_session_registry_snapshot()
        displaced = (entry.get("metadata") or {}).get("taken_over_from") or {}
        assert displaced.get("pid") == foreign_pid and displaced.get("surface") == "tui"

        # And the swap is provable from the taker's side (its own lease is the live one).
        assert active_session_lease_is_registered(record["active_session_lease"]) is True
        record["active_session_lease"].release()
    finally:
        _stop_holder(holder, release)


def test_two_windows_ping_pong_the_session(monkeypatch) -> None:
    """Two live windows: each submit wins and the registry follows — always exactly one entry,
    always the last writer, and the displaced window can see that it lost the slot."""
    monkeypatch.setenv(SESSION_TAKEOVER_ENV, "1")
    window_a, window_b = _record("window-a", source="tui"), _record("window-b", source="tui")

    assert server._request_active_session_takeover(window_a) is True
    assert server._ensure_active_session_slot("window-a", window_a) is None
    assert _registry_owner() == ("window-a", "tui")

    assert server._request_active_session_takeover(window_b) is True
    assert server._ensure_active_session_slot("window-b", window_b) is None
    assert _registry_owner() == ("window-b", "tui")
    assert active_session_lease_is_registered(window_a["active_session_lease"]) is False

    assert server._request_active_session_takeover(window_a) is True
    assert server._ensure_active_session_slot("window-a", window_a) is None
    assert _registry_owner() == ("window-a", "tui")
    assert active_session_lease_is_registered(window_b["active_session_lease"]) is False

    window_a["active_session_lease"].release()


def test_only_interactive_user_submits_may_arm(monkeypatch) -> None:
    """Auto-continue, wake-ups, CLI/batch runs, bot-relayed messages and compute-host children never
    arm takeover — they keep the upstream refusal, so a background turn can never hijack a chat.
    (An unresolved source falls back to this runtime's own platform, i.e. tui here.)"""
    monkeypatch.delenv(SESSION_TAKEOVER_ENV, raising=False)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})  # unset config ⇒ policy default (ON)
    assert server._session_takeover_requested(_record("x", source="tui")) is True
    assert server._session_takeover_requested(_record("x", source="desktop")) is True
    for source in ("cli", "cron", "gateway", "telegram", "wechat", "discord"):
        assert server._session_takeover_requested(_record("x", source=source)) is False
        assert server._request_active_session_takeover(_record("x", source=source)) is False

    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a, **k: True)
    record = _record("x", source="desktop")
    assert server._request_active_session_takeover(record) is False
    assert "_active_session_takeover_armed" not in record


def test_gate_refuses_synthesized_and_relayed_turns(monkeypatch) -> None:
    """The submit gate is the only arming entry point, and it stays closed for anything that is not
    the user typing: hosted-room/terminal-callback turns and bot-relay-authored (another person's)
    messages never seize the chat from the device that has it open."""
    monkeypatch.delenv(SESSION_TAKEOVER_ENV, raising=False)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    hosted = _record("x", source="tui")
    assert server._maybe_request_active_session_takeover(hosted, internal_hosted_submit=True) is False
    assert "_active_session_takeover_armed" not in hosted

    relayed = _record("x", source="tui")
    assert server._maybe_request_active_session_takeover(relayed, relay_author=object()) is False
    assert "_active_session_takeover_armed" not in relayed

    typed = _record("x", source="tui")
    assert server._maybe_request_active_session_takeover(typed) is True
    assert typed["_active_session_takeover_armed"] is True


def test_policy_switches_disable_takeover(monkeypatch) -> None:
    """Either switch restores upstream's strict exclusivity, and an unset config falls back to ON
    for the user-submit gate (that fallback is what keeps the original lockout from coming back)."""
    assert resolve_session_takeover({}) is False  # other callers keep upstream's default
    assert resolve_session_takeover({}, default=True) is True
    assert resolve_session_takeover({"active_sessions": {"takeover": False}}, default=True) is False
    assert resolve_session_takeover({"active_sessions": {"takeover": True}}) is True
    assert resolve_session_takeover({"session_takeover": "off"}, default=True) is False

    monkeypatch.setattr(server, "_load_cfg", lambda: {"active_sessions": {"takeover": False}})
    monkeypatch.delenv(SESSION_TAKEOVER_ENV, raising=False)
    record = _record("phone", source="tui")
    assert server._session_takeover_requested(record) is False
    assert server._request_active_session_takeover(record) is False
    assert "_active_session_takeover_armed" not in record

    monkeypatch.setenv(SESSION_TAKEOVER_ENV, "0")
    assert server._session_takeover_requested(_record("phone", source="tui")) is False

    monkeypatch.setenv(SESSION_TAKEOVER_ENV, "1")
    assert server._session_takeover_requested(_record("phone", source="tui")) is True
