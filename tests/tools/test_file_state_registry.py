#!/usr/bin/env python3
"""Tests for the cross-agent FileStateRegistry (tools/file_state.py).

Covers the three layers added for safe concurrent subagent file edits:

  1. Cross-agent staleness detection via ``check_stale``
  2. Per-path serialization via ``lock_path``
  3. Delegate-completion reminder via ``writes_since``

Plus integration through the real ``read_file_tool`` / ``write_file_tool``
/ ``patch_tool`` handlers so the full hook wiring is exercised.

Run:
    python -m pytest tests/tools/test_file_state_registry.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import file_state
from tools.file_tools import (
    clear_file_ops_cache,
    read_file_tool,
    write_file_tool,
)


def _tmp_file(content: str = "initial\n") -> str:
    fd, path = tempfile.mkstemp(prefix="hermes_file_state_test_", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


class FileStateRegistryUnitTests(unittest.TestCase):
    """Direct unit tests on the registry singleton."""

    def setUp(self) -> None:
        file_state.get_registry().clear()
        self._tmpfiles: list[str] = []

    def tearDown(self) -> None:
        for p in self._tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass
        file_state.get_registry().clear()

    def _mk(self, content: str = "x\n") -> str:
        p = _tmp_file(content)
        self._tmpfiles.append(p)
        return p

    def test_record_read_then_check_stale_returns_none(self):
        p = self._mk()
        file_state.record_read("A", p)
        self.assertIsNone(file_state.check_stale("A", p))

    def test_sibling_write_flags_other_agent_as_stale(self):
        p = self._mk()
        file_state.record_read("A", p)
        # Simulate sibling writing this file later
        time.sleep(0.01)  # ensure ts ordering across resolution
        file_state.note_write("B", p)
        warn = file_state.check_stale("A", p)
        self.assertIsNotNone(warn)
        self.assertIn("B", warn)
        self.assertIn("sibling", warn.lower())


    def test_lock_path_serializes_same_path(self):
        p = self._mk()
        events: list[tuple[str, int]] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            with file_state.lock_path(p):
                with lock:
                    events.append(("enter", i))
                time.sleep(0.01)
                with lock:
                    events.append(("exit", i))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Every enter must be immediately followed by its matching exit.
        self.assertEqual(len(events), 8)
        for i in range(0, 8, 2):
            self.assertEqual(events[i][0], "enter")
            self.assertEqual(events[i + 1][0], "exit")
            self.assertEqual(events[i][1], events[i + 1][1])

    def test_lock_path_is_per_path_not_global(self):
        a = self._mk()
        b = self._mk()
        b_entered = threading.Event()

        def hold_a() -> None:
            with file_state.lock_path(a):
                b_entered.wait(timeout=2.0)

        def enter_b() -> None:
            time.sleep(0.02)  # let A grab its lock
            with file_state.lock_path(b):
                b_entered.set()

        ta = threading.Thread(target=hold_a)
        tb = threading.Thread(target=enter_b)
        ta.start()
        tb.start()
        self.assertTrue(b_entered.wait(timeout=3.0))
        ta.join(timeout=3.0)
        tb.join(timeout=3.0)

    def test_lock_path_state_is_released_after_last_waiter(self):
        p = self._mk()
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def first() -> None:
            with file_state.lock_path(p):
                first_entered.set()
                release_first.wait(timeout=2.0)

        def second() -> None:
            first_entered.wait(timeout=2.0)
            with file_state.lock_path(p):
                second_entered.set()

        ta = threading.Thread(target=first)
        tb = threading.Thread(target=second)
        ta.start()
        tb.start()
        self.assertTrue(first_entered.wait(timeout=2.0))
        time.sleep(0.02)
        self.assertFalse(second_entered.is_set())
        release_first.set()
        ta.join(timeout=3.0)
        tb.join(timeout=3.0)

        registry = file_state.get_registry()
        self.assertTrue(second_entered.is_set())
        self.assertNotIn(p, registry._path_locks)
        self.assertNotIn(p, registry._path_lock_users)

    def test_clear_file_ops_cache_releases_task_state(self):
        p = self._mk()
        task_id = "finished-task"
        file_state.record_read(task_id, p)

        from tools import file_tools_read_tracking as rt

        rt._read_tracker[task_id] = {"dedup": {}}
        rt._patch_failure_tracker[task_id] = {p: 2}

        clear_file_ops_cache(task_id)

        self.assertEqual(file_state.known_reads(task_id), [])
        self.assertNotIn(task_id, rt._read_tracker)
        self.assertNotIn(task_id, rt._patch_failure_tracker)

    def test_forget_task_clears_last_writer_claims(self):
        """A finished task is not a concurrent sibling: forget_task must drop its writer
        claims so the next run of the same job (fresh ``cron:<job>:<uuid>`` id) can write
        the same scratch path without a "modified by sibling subagent" refusal."""
        p = self._mk()
        file_state.note_write("cron:JOB:run1", p)
        file_state.get_registry().forget_task("cron:JOB:run1")

        self.assertIsNone(file_state.check_stale("cron:JOB:run2", p))
        # A sibling that has NOT ended still triggers the guard.
        file_state.note_write("subagent-1-live", p)
        self.assertIn("sibling subagent 'subagent-1-live'", file_state.check_stale("cron:JOB:run2", p))

    def test_agent_close_forgets_every_task_id_it_ran(self):
        """``AIAgent.close()`` receives the session_id, but file tools key the registry by
        the per-turn task_id (cron ``cron:<job>:<uuid>``, subagent ``subagent-N-xxxx``).
        close() must release the file state of every task id the agent ran."""
        p = self._mk()
        file_state.record_read("cron:JOB:run1", p)
        file_state.note_write("cron:JOB:run1", p)
        with patch("run_agent.AIAgent.__init__", return_value=None):
            from run_agent import AIAgent
            agent = AIAgent.__new__(AIAgent)
            agent.session_id = "cron_JOB_20260918_060000"
            agent._process_owner_task_ids = {"cron:JOB:run1"}
            agent._active_children = []
            agent._active_children_lock = threading.Lock()
            agent.client = None
            with patch("run_agent.cleanup_vm"), patch("run_agent.cleanup_browser"), \
                 patch("tools.computer_use.tool.release_computer_use_session"):
                agent.close()

        self.assertEqual(file_state.known_reads("cron:JOB:run1"), [])
        self.assertIsNone(file_state.check_stale("cron:JOB:run2", p))

    def test_kill_switch_env_var(self):
        p = self._mk()
        os.environ["HERMES_DISABLE_FILE_STATE_GUARD"] = "1"
        try:
            file_state.record_read("A", p)
            file_state.note_write("B", p)
            self.assertIsNone(file_state.check_stale("A", p))
            self.assertEqual(file_state.known_reads("A"), [])
            self.assertEqual(
                file_state.writes_since("A", 0.0, [p]),
                {},
            )
        finally:
            del os.environ["HERMES_DISABLE_FILE_STATE_GUARD"]


class FileToolsIntegrationTests(unittest.TestCase):
    """Integration through the real file_tools handlers.

    These exercise the wiring: read_file_tool → registry.record_read,
    write_file_tool / patch_tool → check_stale + lock_path + note_write.
    """

    def setUp(self) -> None:
        file_state.get_registry().clear()
        self._tmpdir = tempfile.mkdtemp(prefix="hermes_file_state_int_")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        file_state.get_registry().clear()

    def _write_seed(self, name: str, content: str = "seed\n") -> str:
        p = os.path.join(self._tmpdir, name)
        with open(p, "w") as f:
            f.write(content)
        return p

    def test_sibling_agent_write_refuses_stale_overwrite_through_handler(self):
        p = self._write_seed("shared.txt")
        r = json.loads(read_file_tool(path=p, task_id="agentA"))
        self.assertNotIn("error", r)

        self.assertNotIn("error", json.loads(read_file_tool(path=p, task_id="agentB")))
        w_b = json.loads(write_file_tool(path=p, content="B wrote\n", task_id="agentB"))
        self.assertNotIn("error", w_b)

        w_a = json.loads(write_file_tool(path=p, content="A stale\n", task_id="agentA"))
        err = w_a.get("error", "")
        self.assertTrue(w_a.get("stale_write_blocked"), f"expected stale write refusal, got: {w_a}")
        # The cross-agent message names the sibling task_id; B's write survives.
        self.assertIn("agentB", err)
        self.assertIn("sibling", err.lower())
        with open(p) as f:
            self.assertEqual(f.read(), "B wrote\n")




class OtherSessionActivityTests(unittest.TestCase):
    """other_session_activity(): read-time multi-session awareness (2026-10-24)."""

    def setUp(self) -> None:
        file_state.get_registry().clear()
        self._tmpfiles: list[str] = []

    def tearDown(self) -> None:
        for p in self._tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass
        file_state.get_registry().clear()

    def _mk(self, content: str = "x\n") -> str:
        p = _tmp_file(content)
        self._tmpfiles.append(p)
        return p

    def test_write_activity_reported(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        reg.record_read("session:A", p)
        reg.note_write("session:A", p)
        act = reg.other_session_activity(p, "session:B")
        self.assertIsNotNone(act)
        self.assertEqual(act["kind"], "write")
        self.assertEqual(act["task_id"], "session:A")

    def test_read_activity_reported(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        reg.record_read("session:A", p)
        act = reg.other_session_activity(p, "session:B")
        self.assertIsNotNone(act)
        self.assertEqual(act["kind"], "read")
        self.assertEqual(act["task_id"], "session:A")

    def test_own_activity_excluded(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        reg.note_write("session:A", p)
        self.assertIsNone(reg.other_session_activity(p, "session:A"))

    def test_unknown_path_returns_none(self) -> None:
        self.assertIsNone(
            file_state.get_registry().other_session_activity("/no/such/path.txt", "session:B"))

    def test_write_outranks_read(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        reg.record_read("session:A", p)
        reg.note_write("session:B", p)
        reg.record_read("session:C", p)
        act = reg.other_session_activity(p, "session:D")
        self.assertEqual(act["kind"], "write")
        self.assertEqual(act["task_id"], "session:B")

    def test_stale_write_ignored(self) -> None:
        reg = file_state.get_registry()
        reg._last_writer["/tmp/stale-write.txt"] = ("session:Z", time.time() - 3600)
        self.assertIsNone(reg.other_session_activity("/tmp/stale-write.txt", "session:B"))

    def test_stale_read_ignored(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        mtime = os.path.getmtime(p)
        reg._reads["session:Y"] = {p: (mtime, time.time() - 1200, False)}
        self.assertIsNone(reg.other_session_activity(p, "session:B"))

    def test_guard_kill_switch(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        reg.note_write("session:A", p)
        with patch.dict(os.environ, {"HERMES_DISABLE_FILE_STATE_GUARD": "1"}):
            self.assertIsNone(reg.other_session_activity(p, "session:B"))

    def test_hint_kill_switch(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        reg.note_write("session:A", p)
        with patch.dict(os.environ, {"HERMES_DISABLE_FILE_CONFLICT_HINT": "1"}):
            self.assertIsNone(reg.other_session_activity(p, "session:B"))

    def test_forget_task_clears(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        reg.note_write("session:A", p)
        reg.forget_task("session:A")
        self.assertIsNone(reg.other_session_activity(p, "session:B"))


class ActivityPanelSnapshotTests(unittest.TestCase):
    """_maybe_snapshot(): owner-facing activity snapshot + panel script (2026-10-24)."""

    def setUp(self) -> None:
        file_state.get_registry().clear()
        self._home = tempfile.mkdtemp(prefix="hermes_activity_panel_")
        self._env = patch.dict(os.environ, {"HERMES_HOME": self._home})
        self._env.start()
        self._interval = patch.object(file_state, "_SNAPSHOT_MIN_INTERVAL", 0.0)
        self._interval.start()
        self._tmpfiles: list[str] = []

    def tearDown(self) -> None:
        self._interval.stop()
        self._env.stop()
        for p in self._tmpfiles:
            try:
                os.unlink(p)
            except OSError:
                pass
        shutil.rmtree(self._home, ignore_errors=True)
        file_state.get_registry().clear()

    def _mk(self, content: str = "x\n") -> str:
        p = _tmp_file(content)
        self._tmpfiles.append(p)
        return p

    def _snap(self) -> Path:
        return Path(self._home) / f"file-activity.{os.getpid()}.json"

    def test_snapshot_written_on_activity(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        reg.record_read("session:A", p)
        reg.note_write("session:A", p)
        snap = self._snap()
        self.assertTrue(snap.exists(), "activity should produce a snapshot file")
        data = json.loads(snap.read_text(encoding="utf-8"))
        self.assertEqual(data["pid"], os.getpid())
        pairs = {(e["kind"], e["task_id"]) for e in data["entries"]}
        self.assertIn(("write", "session:A"), pairs)
        self.assertIn(("read", "session:A"), pairs)

    def test_snapshot_records_own_activity_for_the_owner(self) -> None:
        # The panel is for the OWNER: unlike the read-time hint, own activity
        # must appear — the owner needs to see every session, including one's own.
        reg = file_state.get_registry()
        p = self._mk()
        reg.note_write("session:A", p)
        data = json.loads(self._snap().read_text(encoding="utf-8"))
        self.assertTrue(any(e["task_id"] == "session:A" for e in data["entries"]))

    def test_snapshot_kill_switch(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        with patch.dict(os.environ, {"HERMES_FILE_ACTIVITY_SNAPSHOT": "1"}):
            reg.note_write("session:A", p)
            self.assertTrue(self._snap().exists())
        os.unlink(self._snap())
        with patch.dict(os.environ, {"HERMES_FILE_ACTIVITY_SNAPSHOT": "0"}):
            reg.note_write("session:A", p)
            self.assertFalse(self._snap().exists())

    def test_panel_script_renders(self) -> None:
        reg = file_state.get_registry()
        p = self._mk()
        reg.note_write("session:abcdef1234567890", p)
        script = Path(__file__).resolve().parents[2] / "scripts" / "file_activity_panel.py"
        self.assertTrue(script.exists(), f"panel script missing: {script}")
        out = subprocess.run([sys.executable, str(script), "--home", self._home],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("文件活动面板", out.stdout)
        self.assertIn("session:abcdef1234567890", out.stdout)
        self.assertIn(p, out.stdout)

    @unittest.skipUnless(sys.platform.startswith("linux"), "linux /proc liveness only")
    def test_panel_cleans_stale_dead_process_files(self) -> None:
        dead = 999999  # a PID that does not exist
        stale = Path(self._home) / f"file-activity.{dead}.json"
        old = time.time() - 7200
        stale.write_text(json.dumps({
            "pid": dead, "updated": old,
            "entries": [{"path": "/tmp/ghost.txt", "task_id": "session:GHOST",
                         "kind": "write", "ts": old}]}), encoding="utf-8")
        os.utime(stale, (old, old))
        script = Path(__file__).resolve().parents[2] / "scripts" / "file_activity_panel.py"
        out = subprocess.run([sys.executable, str(script), "--home", self._home],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(stale.exists(), "stale dead-process snapshot should be cleaned")


if __name__ == "__main__":
    unittest.main()
