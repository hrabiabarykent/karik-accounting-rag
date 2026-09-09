from datetime import datetime, timedelta, timezone
import uuid

from accounting.db import repository
from accounting.models import AttemptStatus


def new_attempt():
    return repository.create_document_and_attempt_atomically(
        'tenant', 'operator', 'missing.xml', 'missing-source-for-regression.xml',
        'application/xml', uuid.uuid4().hex)


def test_recent_publication_of_old_document_is_not_recovered():
    _, attempt, event = new_attempt()
    old = repository._format_dt(datetime.now(timezone.utc) - timedelta(days=1))
    with repository.get_connection() as conn:
        conn.cursor().execute(repository._format_sql('UPDATE processing_attempts SET created_at=%s WHERE id=%s'), (old, attempt.id))
    repository.mark_outbox_event_sent(event.id)
    assert attempt.id not in repository.recover_stuck_attempts()


def test_missing_source_is_terminal_and_not_requeued():
    import tasks
    doc, attempt, event = new_attempt()
    result = tasks.process_invoice_task(attempt.id, doc.storage_path, doc.tenant_id)
    assert result['error'] == 'FILE_NOT_FOUND'
    assert repository.get_attempt(attempt.id).status == AttemptStatus.FAILED
    assert repository.get_outbox_event(event.id).status == 'FAILED'
    assert attempt.id not in repository.recover_stuck_attempts(timeout_seconds=0)


def test_transient_failure_has_backoff_and_fences_old_worker(monkeypatch):
    import tasks
    doc, attempt, event = new_attempt()
    def timeout(*args):
        raise TimeoutError('temporary local service failure')
    monkeypatch.setattr(tasks, '_execute_invoice_pipeline', timeout)
    result = tasks.process_invoice_task(attempt.id, doc.storage_path, doc.tenant_id)
    assert result['retryable'] is True
    assert repository.get_attempt(attempt.id).status == AttemptStatus.PENDING
    assert repository.get_attempt(attempt.id).lease_token is None
    outbox = repository.get_outbox_event(event.id)
    assert outbox.status == 'RETRY'
    assert outbox.retry_count == 1
    assert event.id not in [e.id for e in repository.get_pending_outbox_events()]


def test_permanent_exception_does_not_leave_running_attempt(monkeypatch):
    import tasks
    doc, attempt, event = new_attempt()
    def invalid(*args):
        raise ValueError('invalid local configuration')
    monkeypatch.setattr(tasks, '_execute_invoice_pipeline', invalid)
    tasks.process_invoice_task(attempt.id, doc.storage_path, doc.tenant_id)
    assert repository.get_attempt(attempt.id).status == AttemptStatus.FAILED
    assert repository.get_outbox_event(event.id).status == 'FAILED'


def test_failure_from_stale_worker_cannot_change_new_lease():
    _, attempt, _ = new_attempt()
    token = repository.claim_attempt_atomically(attempt.id, 'current')
    assert repository.fail_attempt(attempt.id, 'obsolete', 'late failure') is False
    assert repository.get_attempt(attempt.id).lease_token == token

