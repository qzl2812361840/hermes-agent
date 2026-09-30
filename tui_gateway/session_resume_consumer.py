"""Resume spool consumer: run queued "continue the unfinished task" prompts IN-PROCESS.

An external scheduler (``/usr/local/bin/ganiu-resume``) drops marker JSON files into a spool
directory (``HERMES_RESUME_SPOOL_DIR``) when a session has been idle past its grace period. The
dashboard gateway polls that directory and — when the session is LIVE in THIS process — submits
the note through the normal prompt pipeline (``_run_prompt_submit``), so the turn shares the
session's runtime, history lock, lease arbitration and fanout mirroring instead of racing a
second ``hermes -z`` CLI for it. Markers whose session is not live here fall back to the external
CLI spawn (same as the scheduler's own path) after a grace window.

Everything is env-gated: no ``HERMES_RESUME_SPOOL_DIR`` → the poller never starts and behaviour
is exactly upstream's. Bodies are rebound onto server.py's globals at install time
(method_ctx.bind_module), so they reference server.py globals bare.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sqlite3
import threading
import time
from pathlib import Path

from tui_gateway._env import env_float

from .method_ctx import bind_module


RESUME_SPOOL_DIR = os.environ.get("HERMES_RESUME_SPOOL_DIR", "").strip()
RESUME_POLL_S = env_float("HERMES_RESUME_CONSUMER_POLL_S", 5.0)
RESUME_GRACE_S = env_float("HERMES_RESUME_CONSUMER_GRACE_S", 60.0)
RESUME_ENABLED = os.environ.get("HERMES_RESUME_CONSUMER_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off", "")
RESUME_EXTERNAL_HERMES = os.environ.get("HERMES_RESUME_EXTERNAL_HERMES", "/usr/local/bin/hermes")
RESUME_NOTIFY_DIR = Path(os.environ.get("GANIU_TASKS_NOTIFY_DIR", "/d/ganiu-tasks/notify"))
RESUME_FALLBACK_SCRIPT = os.environ.get("GANIU_RESUME_SCRIPT", "/usr/local/bin/ganiu-resume")


def _resume_note(task: str) -> str:
    """Same wording the external scheduler uses, so both paths read identically in the transcript."""
    return (
        "[自动续跑] 回到刚才没做完的任务，继续做完。\n\n"
        "[System note: 主人刚才插进来问的问题**已经答完**了。现在回到之前未完成的任务，继续做完。]\n\n"
        f"未完成的任务：{task}\n\n"
        "要求：①先判断这件事现在是否还该干、是否已经做完——不该干或已完成就直接汇报、不要硬做；"
        "②核实当前进度（做完的部分不重做），然后把它做完；"
        "做完在回复里汇报结果（做了什么 + 关键数字/证据）。"
        "护栏：如果这个任务需要主人拍板、或涉及删除数据/付款/对外发消息/改密码等红线动作，"
        "就先停下把情况汇报给主人，绝不擅自动手。"
    )


def _resume_lease_active(home: Path, sid: str, now: float) -> bool | None:
    """True = another run holds the turn lease (skip); None = no lease table / unreadable db."""
    try:
        conn = sqlite3.connect(f"file:{Path(home) / 'state.db'}?mode=ro", uri=True, timeout=3)
        try:
            row = conn.execute(
                "SELECT 1 FROM session_turn_leases WHERE conversation_id = ? AND expires_at > ? LIMIT 1",
                (sid, now)).fetchone()
        finally:
            conn.close()
        return bool(row)
    except Exception:
        return None


def _resume_live_session(sid: str, home: str) -> tuple[str, dict] | None:
    """The live in-process session for a marker: ui-sid hit or profile-scoped session_key match."""
    with _sessions_lock:
        direct = _sessions.get(sid)
        if direct is not None and not direct.get("_finalized") and \
                (direct.get("profile_home") or _hermes_home) == home:
            return sid, direct
    found = _find_live_session_by_key(sid, home)
    if found is None:
        found = _find_live_session_by_key(sid, _ANY_PROFILE)  # stored home may drift from session_key
        if found is not None and (found[1].get("profile_home") or _hermes_home) != home:
            found = None
    return found


def _resume_agent_ready(session: dict) -> bool:
    ready = session.get("agent_ready")
    if ready is not None and hasattr(ready, "is_set"):
        return ready.is_set()
    return session.get("agent") is not None


def _resume_notify(name: str, body: str) -> None:
    with contextlib.suppress(OSError):
        RESUME_NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
        (RESUME_NOTIFY_DIR / name).write_text(body, encoding="utf-8")


def _resume_record_fired(spool: Path, sid: str, d: dict, how: str, pid: int | None, log_path: Path) -> None:
    fired_dir = spool / "fired"
    with contextlib.suppress(OSError):
        fired_dir.mkdir(parents=True, exist_ok=True)
        d = dict(d, triggered=True, triggered_at=time.time(), route=how, pid=pid)
        (fired_dir / f"{sid}_{int(time.time())}.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _resume_run_external(home: str, sid: str, note: str, spool: Path, d: dict) -> None:
    """Not live in this process → the proven CLI path (scheduler parity), watch child included."""
    run_dir = spool / "run"
    with contextlib.suppress(OSError):
        run_dir.mkdir(parents=True, exist_ok=True)
    outlog = run_dir / f"{sid}.log"
    p = subprocess.Popen(
        [RESUME_EXTERNAL_HERMES, "-z", note, "-r", sid],
        stdin=subprocess.DEVNULL, stdout=open(outlog, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT, env={**os.environ, "HERMES_HOME": home},
        start_new_session=True, close_fds=True)
    pfile = run_dir / f"{sid}.pid"
    with contextlib.suppress(OSError):
        pfile.write_text(str(p.pid))
    with contextlib.suppress(Exception, OSError):
        wlog = open(Path("/var/log/ganiu-resume.log"), "a", encoding="utf-8")
        subprocess.Popen(
            ["/usr/bin/python3", RESUME_FALLBACK_SCRIPT, "_watch", str(p.pid), sid, home],
            stdin=subprocess.DEVNULL, stdout=wlog, stderr=subprocess.STDOUT,
            env={**os.environ}, start_new_session=True, close_fds=True)
    _resume_record_fired(spool, sid, d, "external", p.pid, outlog)
    _resume_notify(
        f"resume_start_{int(time.time())}.txt",
        "【自动续跑·已触发(外部兜底,会话不在本网关)】\n"
        f"会话：{sid}\n续跑进程：pid {p.pid}｜日志 {outlog}\n任务：{str(d.get('task'))[:200]}\n")
    logger.info("resume-consumer: external fallback for %s (pid %s)", sid, p.pid)


def _resume_process_marker(spool: Path, mf: Path, now: float) -> None:
    """One marker: claim → run in-process (live) / external fallback (cold) → record."""
    try:
        d = json.loads(mf.read_text(encoding="utf-8"))
    except Exception as ex:
        logger.warning("resume-consumer: unreadable marker %s: %s", mf, ex)
        with contextlib.suppress(OSError):
            mf.unlink(missing_ok=True)
        return
    sid = str(d.get("sid") or mf.stem)
    home = str(d.get("home") or _hermes_home)
    task = str(d.get("task") or "").strip()
    armed = float(d.get("armed_at") or 0)
    expires = float(d.get("expires_at") or 0)
    if not task:
        with contextlib.suppress(OSError):
            mf.unlink(missing_ok=True)
        return
    if expires and now > expires:
        logger.info("resume-consumer: expired marker %s", sid)
        with contextlib.suppress(OSError):
            mf.unlink(missing_ok=True)
        return
    if _resume_lease_active(Path(home), sid, now) is True:
        return  # busy turn; retry next poll (external scheduler skips busy sessions the same way)
    live = _resume_live_session(sid, home)
    note = _resume_note(task)
    claiming = spool / ".claiming" / mf.name
    if live is not None and _resume_agent_ready(live[1]):
        claiming.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(mf, claiming)  # atomic claim before touching the live session
        except OSError:
            return  # another consumer claimed it first — never double-submit
        rid = f"__resume__{int(time.time() * 1000)}"
        sid_live, session = live

        def _run():
            try:
                ok = _run_prompt_submit(rid, sid_live, session, note, display_kind="auto_continue")
                if ok:
                    _resume_record_fired(spool, sid, d, "inproc", None, Path("/dev/null"))
                    _resume_notify(
                        f"resume_start_{int(time.time())}.txt",
                        "【自动续跑·已由网关进程内接管】\n"
                        f"会话：{sid}（live in dashboard,走原生turn管线）\n"
                        f"任务：{task[:200]}\n主人回到会话即看到它接着跑。\n")
                    logger.info("resume-consumer: in-process turn submitted for %s", sid)
                else:
                    # refused (lease raced away / closing) → unclaim so the next poll retries
                    with contextlib.suppress(OSError):
                        os.replace(claiming, mf)
                    logger.info("resume-consumer: submit refused for %s, marker restored", sid)
            except Exception as exc:
                logger.warning("resume-consumer: in-process turn failed for %s: %s", sid, exc)
                with contextlib.suppress(OSError):
                    os.replace(claiming, mf)
        if _start_session_work(_run, name=f"resume-consumer-{sid}") is None:
            with contextlib.suppress(OSError):
                os.replace(claiming, mf)
        return
    # cold session: wait the grace window (it may be mid-build / another client about to open it)
    if now - armed < RESUME_GRACE_S:
        return
    claiming.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(mf, claiming)
    except OSError:
        return  # another consumer claimed it first — never double-submit
    _resume_run_external(home, sid, note, spool, d)


def _resume_sweep_claims(spool: Path) -> None:
    """Startup orphan sweep: claims stranded by a restart go back to the spool."""
    claiming = spool / ".claiming"
    if not claiming.exists():
        return
    for f in list(claiming.glob("*.json")):
        with contextlib.suppress(OSError):
            os.replace(f, spool / f.name)
    with contextlib.suppress(OSError):
        claiming.rmdir()


def _resume_consumer_loop(spool: Path) -> None:
    _resume_sweep_claims(spool)
    while True:
        time.sleep(RESUME_POLL_S)
        try:
            now = time.time()
            for mf in sorted(spool.glob("*.json")):
                with contextlib.suppress(Exception):
                    _resume_process_marker(spool, mf, now)
        except Exception as exc:
            logger.debug("resume-consumer scan error: %s", exc)


def _start_resume_consumer() -> None:
    if not (RESUME_ENABLED and RESUME_SPOOL_DIR):
        return
    spool = Path(RESUME_SPOOL_DIR)
    with contextlib.suppress(OSError):
        spool.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_resume_consumer_loop, args=(spool,),
                     name="resume-consumer", daemon=True).start()
    logger.info("resume spool consumer started: dir=%s poll=%.0fs grace=%.0fs",
                spool, RESUME_POLL_S, RESUME_GRACE_S)


def register(server) -> None:
    """Publish this module's helpers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
    # Rebound bodies resolve names against server globals; stdlib helpers this module imports may
    # not exist there (e.g. pathlib.Path) — publish ours (same objects) for any missing name.
    _g = vars(server)
    for _name in ("Path", "json", "os", "time", "sqlite3", "subprocess", "contextlib", "threading"):
        if _name not in _g:
            _g[_name] = globals()[_name]
    if os.environ.get("HERMES_RESUME_CONSUMER_STARTED") != "1":  # module import ≠ server boot; start once
        os.environ["HERMES_RESUME_CONSUMER_STARTED"] = "1"
        server._start_resume_consumer()  # the rebound copy: thread chain must see server globals
