from __future__ import annotations

import pytest

from wiki_ai.investigation.tasks import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransition,
    Lease,
    LeaseError,
    LeaseExpired,
    LeaseHeld,
    LeaseNotHeld,
    MissingResult,
    Task,
    TaskState,
    acquire,
    holds,
    release,
    renew,
    reset_attempts,
    transition,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_task(state: TaskState = TaskState.PENDING, **kwargs: object) -> Task:
    return Task(
        task_id="t1",
        objective_hash="obj",
        snapshot_hash="snap",
        state=state,
        **kwargs,
    )


def test_transition_table_covers_every_state():
    assert set(ALLOWED_TRANSITIONS) == set(TaskState)


def test_no_state_transitions_to_itself():
    assert all(state not in targets for state, targets in ALLOWED_TRANSITIONS.items())


def test_terminal_states_only_reopen_as_pending():
    assert all(ALLOWED_TRANSITIONS[s] == frozenset({TaskState.PENDING}) for s in TERMINAL_STATES)


def test_legal_path_pending_ready_leased_succeeded():
    task = make_task()
    task = transition(task, TaskState.READY, "deps satisfeitas")
    task = transition(task, TaskState.LEASED, "despachada")
    task = transition(task, TaskState.SUCCEEDED, "resultado aceito", result_hash="h1")
    assert task.state is TaskState.SUCCEEDED
    assert task.result_hash == "h1"


def test_illegal_transition_is_typed_and_carries_endpoints():
    task = make_task()
    with pytest.raises(IllegalTransition) as info:
        transition(task, TaskState.SUCCEEDED, "atalho")
    assert info.value.source is TaskState.PENDING
    assert info.value.target is TaskState.SUCCEEDED
    assert info.value.task_id == "t1"


def test_illegal_transition_does_not_mutate_task():
    task = make_task(state=TaskState.SUCCEEDED, result_hash="h1")
    with pytest.raises(IllegalTransition):
        transition(task, TaskState.LEASED, "reexecutar")
    assert task.state is TaskState.SUCCEEDED


def test_succeeded_requires_result_hash():
    task = transition(
        transition(make_task(), TaskState.READY, "pronta"), TaskState.LEASED, "despachada"
    )
    with pytest.raises(MissingResult):
        transition(task, TaskState.SUCCEEDED, "sem resultado")


def test_leasing_counts_attempt_and_requeue_clears_result():
    task = transition(make_task(), TaskState.READY, "pronta")
    task = transition(task, TaskState.LEASED, "despachada")
    assert task.attempt == 1
    task = transition(task, TaskState.SUCCEEDED, "aceito", result_hash="h1")
    task = transition(task, TaskState.PENDING, "snapshot mudou")
    assert task.result_hash == ""
    assert task.attempt == 1
    assert reset_attempts(task).attempt == 0


def test_acquire_rejects_non_positive_ttl():
    clock = FakeClock()
    with pytest.raises(LeaseError):
        acquire("t1", "w1", ttl_seconds=0.0, clock=clock)


def test_acquire_uses_clock_and_never_wall_time():
    clock = FakeClock(start=500.0)
    lease = acquire("t1", "w1", ttl_seconds=30.0, clock=clock)
    assert lease.acquired_at == 500.0
    assert lease.expires_at == 530.0


def test_second_owner_is_refused_while_lease_is_live():
    clock = FakeClock()
    first = acquire("t1", "w1", ttl_seconds=30.0, clock=clock)
    clock.advance(29.0)
    with pytest.raises(LeaseHeld) as info:
        acquire("t1", "w2", ttl_seconds=30.0, clock=clock, current=first)
    assert info.value.owner == "w1"


def test_expired_lease_is_reacquired_by_another_owner():
    clock = FakeClock()
    first = acquire("t1", "w1", ttl_seconds=30.0, clock=clock)
    clock.advance(30.0)
    second = acquire("t1", "w2", ttl_seconds=30.0, clock=clock, current=first)
    assert second.owner == "w2"
    assert second.token != first.token


def test_expired_lease_cannot_be_renewed_by_the_same_token():
    clock = FakeClock()
    lease = acquire("t1", "w1", ttl_seconds=30.0, clock=clock)
    clock.advance(30.0)
    with pytest.raises(LeaseExpired) as info:
        renew(lease, lease.token, ttl_seconds=30.0, clock=clock)
    assert info.value.token == lease.token


def test_renew_extends_from_the_current_instant():
    clock = FakeClock()
    lease = acquire("t1", "w1", ttl_seconds=30.0, clock=clock)
    clock.advance(10.0)
    renewed = renew(lease, lease.token, ttl_seconds=30.0, clock=clock)
    assert renewed.expires_at == 1040.0
    assert renewed.token == lease.token


def test_renew_with_foreign_token_is_refused():
    clock = FakeClock()
    lease = acquire("t1", "w1", ttl_seconds=30.0, clock=clock)
    with pytest.raises(LeaseNotHeld):
        renew(lease, "outro", ttl_seconds=30.0, clock=clock)


def test_released_lease_is_expired_and_not_renewable():
    clock = FakeClock()
    lease = acquire("t1", "w1", ttl_seconds=30.0, clock=clock)
    released = release(lease, lease.token, clock=clock)
    assert released.is_expired(clock())
    with pytest.raises(LeaseExpired):
        renew(released, released.token, ttl_seconds=30.0, clock=clock)


def test_release_is_idempotent_and_rejects_foreign_token():
    clock = FakeClock()
    lease = acquire("t1", "w1", ttl_seconds=30.0, clock=clock)
    released = release(lease, lease.token, clock=clock)
    assert release(released, released.token, clock=clock) == released
    with pytest.raises(LeaseNotHeld):
        release(released, "outro", clock=clock)


def test_holds_only_for_live_lease_with_matching_token():
    clock = FakeClock()
    lease = acquire("t1", "w1", ttl_seconds=30.0, clock=clock)
    assert holds(lease, lease.token, clock())
    assert not holds(lease, "outro", clock())
    assert not holds(None, lease.token, clock())
    clock.advance(30.0)
    assert not holds(lease, lease.token, clock())


def test_remaining_never_goes_negative():
    lease = Lease("t1", "w1", 0.0, 10.0, "tok")
    assert lease.remaining(4.0) == 6.0
    assert lease.remaining(99.0) == 0.0
