"""ganiu (2026-09-29): a real inbound chat message may take a session over from another surface.

Same product policy as the TUI gate, on the messaging-gateway path (the phone / App side talks to
Hermes through this one): the chat follows whoever is typing, so the device the person just typed
on becomes the owner instead of being refused with "This chat is open in another Hermes
window/terminal". Internal events (background-process notifications, wake-ups, replayed system
messages) must keep upstream's refusal, so an automated turn can never displace a person's open
chat, and the env / ``active_sessions.takeover`` policy still wins.

These tests pin: refusal without the arm, seizure with it (across a real process boundary),
the policy switch, and the wiring at the inbound call site (``arm_takeover=not is_internal``).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.run_busy import GatewayBusySessionMixin
from hermes_cli.active_sessions import (
    SESSION_NOT_OWNED,
    SESSION_TAKEOVER_ENV,
    active_session_registry_snapshot,
)

SESSION_KEY = "gateway-chat"

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


class _Gateway(GatewayBusySessionMixin):
    """The minimum surface ``_claim_active_session_slot`` actually touches."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config if config is not None else {}

    def _is_session_running(self, session_key: str) -> bool:  # noqa: ARG002 - registry owns it
        return False

    def _get_max_concurrent_sessions(self) -> int | None:
        return None

    def _running_agent_count(self) -> int:
        return 0


def _source(platform: str = "telegram") -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value=platform), chat_id="4242", user_id="u-1"
    )


def _spawn_lease_holder(tmp_path: Path, session_id: str) -> tuple[subprocess.Popen, Path]:
    """A live FOREIGN process holding the session's lease (the server TUI / another window)."""
    ready, release = tmp_path / "ready", tmp_path / "release"
    env = dict(os.environ, SESSION_ID=session_id, READY_FILE=str(ready), RELEASE_FILE=str(release))
    child = subprocess.Popen(
        [sys.executable, "-c", _LEASE_HOLDER], env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 60
    while not ready.exists():
        if child.poll() is not None:
            raise AssertionError(f"lease holder died: {child.stderr.read() if child.stderr else ''}")
        if time.monotonic() >= deadline:
            raise AssertionError("lease holder never became ready")
        time.sleep(0.02)
    return child, release


def _stop_holder(child: subprocess.Popen, release: Path) -> None:
    release.write_text("go", encoding="utf-8")
    try:
        child.wait(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()


def _registry_owner() -> tuple[str, str, int | None]:
    """(session_id, surface, pid) of the single registry entry."""
    (entry,) = active_session_registry_snapshot()
    return (
        str(entry.get("session_id") or ""),
        str(entry.get("surface") or ""),
        entry.get("pid"),
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(SESSION_TAKEOVER_ENV, raising=False)


def test_internal_event_keeps_upstream_refusal(tmp_path: Path) -> None:
    """No arm (internal / automated event) ⇒ the live foreign owner still refuses us."""
    holder, release = _spawn_lease_holder(tmp_path, SESSION_KEY)
    try:
        lease, message = _Gateway()._claim_active_session_slot(SESSION_KEY, _source())
        assert lease is None and message is not None
        assert getattr(message, "reason", None) == SESSION_NOT_OWNED
        assert _registry_owner() == (SESSION_KEY, "tui", holder.pid)
    finally:
        _stop_holder(holder, release)


def test_real_inbound_message_takes_the_session_over(tmp_path: Path) -> None:
    """The arm seizes: our lease wins, the surface is recorded, and the displaced owner is named."""
    holder, release = _spawn_lease_holder(tmp_path, SESSION_KEY)
    try:
        lease, message = _Gateway()._claim_active_session_slot(
            SESSION_KEY, _source(), arm_takeover=True
        )
        assert message is None and lease is not None
        assert _registry_owner() == (SESSION_KEY, "gateway:telegram", os.getpid())
        (entry,) = active_session_registry_snapshot()
        displaced = (entry.get("metadata") or {}).get("taken_over_from") or {}
        assert displaced.get("pid") == holder.pid and displaced.get("surface") == "tui"
        lease.release()
    finally:
        _stop_holder(holder, release)


def test_policy_env_switch_disables_gateway_takeover(tmp_path: Path, monkeypatch) -> None:
    """``HERMES_SESSION_TAKEOVER=0`` restores upstream exclusivity even for a real submit."""
    monkeypatch.setenv(SESSION_TAKEOVER_ENV, "0")
    holder, release = _spawn_lease_holder(tmp_path, SESSION_KEY)
    try:
        lease, message = _Gateway()._claim_active_session_slot(
            SESSION_KEY, _source(), arm_takeover=True
        )
        assert lease is None and getattr(message, "reason", None) == SESSION_NOT_OWNED
        assert _registry_owner() == (SESSION_KEY, "tui", holder.pid)
    finally:
        _stop_holder(holder, release)


def test_config_switch_disables_gateway_takeover(tmp_path: Path) -> None:
    """``active_sessions.takeover: false`` in config.yaml does the same."""
    holder, release = _spawn_lease_holder(tmp_path, SESSION_KEY)
    try:
        lease, message = _Gateway({"active_sessions": {"takeover": False}})._claim_active_session_slot(
            SESSION_KEY, _source(), arm_takeover=True
        )
        assert lease is None and getattr(message, "reason", None) == SESSION_NOT_OWNED
    finally:
        _stop_holder(holder, release)


def test_inbound_call_site_arms_only_for_real_messages() -> None:
    """Wiring pin: the inbound user message path is the ONLY thing that arms the claim."""
    source = Path(__file__).resolve().parents[2] / "gateway" / "run_inbound.py"
    text = source.read_text(encoding="utf-8")
    call = re.search(
        r"_claim_active_session_slot\(\s*\n\s*_quick_key,\s*source,\s*arm_takeover=(?P<armed>[^,\)]+)\)",
        text,
    )
    assert call is not None, "inbound claim call site changed shape — re-verify the takeover arm"
    assert call.group("armed").strip() == "not is_internal"
