"""ticket_stage 单元测试：阶段表、不能自续、自动输入扣住、幂等。"""
from agent.ticket_stage import Report, Ticket, TicketInput, add_input, can_retry, ticket_stage, waits_for_person


def test_stage_priority_order():
    t = Ticket(id="1", report=Report(state="waiting", awaiting_person=True), undelivered=[TicketInput(id="m", human=True)])
    assert ticket_stage(t) == "person"  # person 优先于 waiting
    t2 = Ticket(id="2", running_assignment="a", undelivered=[TicketInput(id="m", human=True)])
    assert ticket_stage(t2) == "waiting"  # waiting 优先于 working
    t3 = Ticket(id="3", running_assignment="a", pending_assignment="b")
    assert ticket_stage(t3) == "working"
    t4 = Ticket(id="4", pending_assignment="b")
    assert ticket_stage(t4) == "queued"
    assert ticket_stage(Ticket(id="5")) == "idle"


def test_person_report_states_only():
    assert ticket_stage(Ticket(id="1", report=Report(state="waiting", awaiting_person=True))) == "person"
    assert ticket_stage(Ticket(id="2", report=Report(state="blocked", awaiting_person=True))) == "person"
    # awaiting_person=False 不算 person
    assert ticket_stage(Ticket(id="3", report=Report(state="failed", awaiting_person=False))) == "idle"


def test_person_cannot_self_continue():
    t = Ticket(id="1", report=Report(state="waiting", awaiting_person=True))
    assert can_retry(t) is False  # person 阶段不能自续
    t2 = Ticket(id="2", report=Report(state="failed", awaiting_person=False))
    assert can_retry(t2) is True
    t3 = Ticket(id="3", report=Report(state="failed", awaiting_person=False), ended="done")
    assert can_retry(t3) is False  # 已结束
    t4 = Ticket(id="4")
    assert can_retry(t4) is False  # 无回报


def test_waits_for_person_and_hold():
    t = Ticket(id="1", report=Report(state="waiting", awaiting_person=True))
    assert waits_for_person(t) is True
    held = add_input(t, TicketInput(id="auto1", automatic=True))
    assert held is True
    assert t.undelivered == [] and len(t.held) == 1  # 被扣住
    human = add_input(t, TicketInput(id="h1", human=True))
    assert human is True
    assert t.undelivered[0].id == "h1"  # 人的消息照常入队


def test_input_idempotent():
    t = Ticket(id="1")
    assert add_input(t, TicketInput(id="a")) is True
    assert add_input(t, TicketInput(id="a")) is False
    assert len(t.undelivered) == 1
