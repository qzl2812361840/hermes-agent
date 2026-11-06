"""proactive_observe 单元测试：三档判定与候选卡台账。"""
from agent.proactive_observe import AcceptMode, Candidate, ObserveLedger, OutcomeKind, assessment_outcome


class Rule:
    def __init__(self, id="r1", strength="inferred"):
        self.id = id
        self.strength = strength


def test_explicit_goes_through():
    out = assessment_outcome(Rule(strength="explicit"), AcceptMode.OBSERVE)
    assert out.kind is OutcomeKind.ACCEPT and out.would_accept is False


def test_inferred_observe_records_would_accept():
    out = assessment_outcome(Rule(), AcceptMode.OBSERVE)
    assert out.kind is OutcomeKind.IGNORE
    assert out.would_accept is True
    assert out.proposal["rule"] == "r1"


def test_inferred_auto_accepts():
    out = assessment_outcome(Rule(), AcceptMode.AUTO)
    assert out.kind is OutcomeKind.ACCEPT and out.would_accept is False


def test_inferred_off_ignores():
    out = assessment_outcome(Rule(), AcceptMode.OFF)
    assert out.kind is OutcomeKind.IGNORE and out.would_accept is True


def test_direction_guard_blocks_auto():
    out = assessment_outcome(Rule(), AcceptMode.AUTO, direction_allowed=False)
    assert out.kind is OutcomeKind.IGNORE and out.would_accept is True


def test_no_rule_ignores():
    out = assessment_outcome(None, AcceptMode.AUTO)
    assert out.kind is OutcomeKind.IGNORE and out.proposal is None


def test_ledger_records_candidates_only():
    ledger = ObserveLedger()
    cand = ledger.record(assessment_outcome(Rule(), AcceptMode.OBSERVE), at_ms=1, suggested_action="发周报")
    assert isinstance(cand, Candidate) and cand.rule_id == "r1" and cand.suggested_action == "发周报"
    assert ledger.record(assessment_outcome(Rule(strength="explicit"), AcceptMode.OBSERVE), at_ms=2) is None
    assert len(ledger.entries) == 1
    cand.accepted = True
    assert ledger.entries[0].accepted is True
