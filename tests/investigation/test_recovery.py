from __future__ import annotations

import pytest

from wiki_ai.agent.envelope import EnvelopeIdentity, StaleReason, StaleResult, seal
from wiki_ai.investigation.recovery import (
    Decision,
    Failure,
    FailureKind,
    InvalidAttempt,
    InvalidPolicy,
    Outcome,
    RetryPolicy,
    from_stale_result,
    next_attempt,
    permanent,
    recover,
    stale,
    transient,
)
from wiki_ai.investigation.tasks import IllegalTransition, Task, TaskState, transition

POLICY = RetryPolicy(max_attempts=3, backoff=(1.0, 5.0))


def leased(attempt: int = 1) -> Task:
    task = Task(task_id="t1", objective_hash="obj", snapshot_hash="snap")
    task = transition(task, TaskState.READY, "pronta")
    task = transition(task, TaskState.LEASED, "despachada")
    for _ in range(attempt - 1):
        task = transition(task, TaskState.PENDING, "retentativa")
        task = transition(task, TaskState.READY, "pronta")
        task = transition(task, TaskState.LEASED, "despachada")
    return task


def test_policy_rejects_impossible_limits():
    with pytest.raises(InvalidPolicy):
        RetryPolicy(max_attempts=0)
    with pytest.raises(InvalidPolicy):
        RetryPolicy(max_attempts=2, backoff=(-1.0,))


def test_next_attempt_requires_a_started_attempt():
    with pytest.raises(InvalidAttempt):
        next_attempt(POLICY, 0)


def test_backoff_is_followed_position_by_position():
    assert next_attempt(POLICY, 1) == Decision(Outcome.RETRY, 1.0, "tentativa 2 de 3")
    assert next_attempt(POLICY, 2) == Decision(Outcome.RETRY, 5.0, "tentativa 3 de 3")


def test_backoff_shorter_than_max_attempts_repeats_the_last_delay():
    policy = RetryPolicy(max_attempts=5, backoff=(2.0,))
    assert next_attempt(policy, 3).delay == 2.0


def test_empty_backoff_means_no_delay():
    assert next_attempt(RetryPolicy(max_attempts=2), 1).delay == 0.0


def test_max_attempts_is_the_hard_stop():
    decision = next_attempt(POLICY, 3)
    assert decision.outcome is Outcome.GIVE_UP
    assert not decision.retry
    assert next_attempt(POLICY, 9).outcome is Outcome.GIVE_UP


def test_failure_kinds_are_data_not_free_text():
    assert transient("timeout").kind is FailureKind.TRANSIENT
    assert permanent("schema_invalid").kind is FailureKind.PERMANENT
    assert stale("snapshot_changed").kind is FailureKind.STALE


def test_fingerprint_ignores_volatile_identifiers():
    first = transient("timeout", "execução a1b2c3d4e5 falhou após 30 s")
    again = transient("timeout", "execução f9e8d7c6b5 falhou após 45 s")
    other = transient("timeout", "conexão recusada")
    assert first.fingerprint == again.fingerprint
    assert first.fingerprint != other.fingerprint


def test_failure_kind_changes_the_fingerprint():
    assert transient("x", "d").fingerprint != permanent("x", "d").fingerprint


def test_stale_result_becomes_a_stale_failure():
    envelope = seal(EnvelopeIdentity("t1", "a", "b", "c"), payload={}, produced_at=1.0)
    failure = from_stale_result(StaleResult(envelope, StaleReason.SNAPSHOT_CHANGED))
    assert failure == Failure(FailureKind.STALE, "snapshot_changed", envelope.envelope_id)


def test_transient_failure_requeues_the_task_with_the_policy_delay():
    result = recover(leased(1), transient("timeout"), POLICY)
    assert result.task.state is TaskState.PENDING
    assert result.decision == Decision(Outcome.RETRY, 1.0, "tentativa 2 de 3")
    assert result.task.attempt == 1


def test_transient_failure_fails_the_task_once_attempts_run_out():
    result = recover(leased(3), transient("timeout"), POLICY)
    assert result.task.state is TaskState.FAILED
    assert result.decision.outcome is Outcome.GIVE_UP


def test_permanent_failure_never_retries():
    result = recover(leased(1), permanent("schema_invalid"), RetryPolicy(max_attempts=9))
    assert result.task.state is TaskState.FAILED
    assert result.decision.outcome is Outcome.GIVE_UP


def test_stale_failure_replans_without_consuming_the_retry_budget():
    result = recover(leased(3), stale("snapshot_changed"), POLICY)
    assert result.task.state is TaskState.PENDING
    assert result.task.attempt == 0
    assert result.decision == Decision(Outcome.RETRY, 0.0, "resultado obsoleto: snapshot_changed")


def test_stale_failure_reopens_an_accepted_result():
    task = transition(leased(1), TaskState.SUCCEEDED, "aceito", result_hash="h1")
    result = recover(task, stale("snapshot_changed"), POLICY)
    assert result.task.state is TaskState.PENDING
    assert result.task.result_hash == ""


def test_recover_has_no_side_effects_on_the_input_task():
    task = leased(1)
    recover(task, transient("timeout"), POLICY)
    assert task.state is TaskState.LEASED
    assert task.attempt == 1


def test_accepted_result_cannot_be_turned_into_failure_without_reopening():
    accepted = transition(leased(1), TaskState.SUCCEEDED, "aceito", result_hash="h1")
    with pytest.raises(IllegalTransition):
        recover(accepted, permanent("schema_invalid"), POLICY)


def test_recovering_a_task_already_back_in_the_pool_is_illegal():
    requeued = recover(leased(1), transient("timeout"), POLICY).task
    with pytest.raises(IllegalTransition):
        recover(requeued, transient("timeout"), POLICY)
