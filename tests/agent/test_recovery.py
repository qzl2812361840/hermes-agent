"""recovery 单测：断点记录、中断标记、检查点续跑、通知防重。"""

import pytest

from agent.recovery import Checkpoint, RunPhase, RunRecovery


@pytest.fixture
def rr(tmp_path):
    r = RunRecovery(str(tmp_path / "rec.db"))
    yield r
    r.close()


def test_begin_checkpoint_end_lifecycle(rr):
    rr.begin("r1", "sync")
    assert rr.get_run("r1").phase is RunPhase.RUNNING
    rr.checkpoint("r1", Checkpoint("batch-28", {"done": 2800}))
    run = rr.get_run("r1")
    assert run.checkpoint.stage == "batch-28"
    assert run.checkpoint.context == {"done": 2800}
    rr.end("r1", ok=True)
    assert rr.get_run("r1").phase is RunPhase.ENDED


def test_recover_marks_orphans_once(rr):
    rr.begin("a", "sync")
    rr.begin("b", "sync")
    interrupted = rr.recover(live_runs={"b"})  # b 还活着
    assert [r.run_id for r in interrupted] == ["a"]
    assert rr.get_run("a").phase is RunPhase.INTERRUPTED
    assert rr.get_run("b").phase is RunPhase.RUNNING
    assert rr.recover() == []  # 一次性防重


def test_resume_from_checkpoint(rr):
    rr.begin("a", "sync")
    rr.checkpoint("a", Checkpoint("batch-28", {"done": 2800}))
    rr.recover()
    got = []
    result = rr.resume("a", lambda cp: got.append(cp) or "resumed")
    assert result == "resumed"
    assert got[0].stage == "batch-28" and got[0].context == {"done": 2800}
    assert rr.get_run("a").phase is RunPhase.RECOVERED


def test_resume_keeps_interrupted_on_failure(rr):
    rr.begin("a", "sync")
    rr.checkpoint("a", Checkpoint("s1"))
    rr.recover()

    def boom(cp):
        raise RuntimeError("cannot resume")

    assert rr.resume("a", boom) is None  # 异常向外抛
    assert rr.get_run("a").phase is RunPhase.INTERRUPTED  # 状态不翻转


def test_resume_only_interrupted(rr):
    rr.begin("a", "sync")  # 还在 running，未 recover
    assert rr.resume("a", lambda cp: "x") is None
    assert rr.resume("ghost", lambda cp: "x") is None


def test_notify_interrupted_dedup(rr):
    calls = []
    rr.begin("a", "sync")
    rr.recover()
    assert rr.notify_interrupted("a", lambda t: calls.append(t) or True) is True
    assert len(calls) == 1
    assert rr.notify_interrupted("a", lambda t: calls.append(t) or True) is True
    assert len(calls) == 1  # 账本防重，不再发


def test_notify_retry_until_delivered(rr):
    calls = []
    rr.begin("a", "sync")
    rr.recover()
    assert rr.notify_interrupted("a", lambda t: False) is False  # 送达失败
    assert rr.notify_interrupted("a", lambda t: calls.append(t) or True) is True
    assert len(calls) == 1


def test_persistence_across_reopen(tmp_path):
    path = str(tmp_path / "r.db")
    rr1 = RunRecovery(path)
    rr1.begin("a", "sync")
    rr1.checkpoint("a", Checkpoint("s1", {"n": 5}))
    rr1.close()
    rr2 = RunRecovery(path)
    run = rr2.get_run("a")
    assert run is not None and run.checkpoint.context == {"n": 5}
    interrupted = rr2.recover()
    assert [r.run_id for r in interrupted] == ["a"]
    rr2.close()


def test_checkpoint_updates_run_in_place(rr):
    rr.begin("a", "sync")
    rr.checkpoint("a", Checkpoint("s1", {"n": 1}))
    rr.checkpoint("a", Checkpoint("s2", {"n": 2}))
    assert rr.get_run("a").checkpoint.stage == "s2"
