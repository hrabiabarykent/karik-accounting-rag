import ast
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from accounting.auth import create_access_token
from accounting.db import AccountingRepository, repository, ERPExportInFlightError, DocumentNotApprovedError
from accounting.models import AISuggestions, DocumentStatus, ExportStatus, ValidationReport

ROOT = Path(__file__).resolve().parents[1]

def eventually(fn, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError('Timed out waiting for integration condition')

def new_document():
    return repository.create_document_and_attempt_atomically('integration', 'auditor', 'test.xml', 'missing.xml', 'application/xml', uuid.uuid4().hex)

def approved_document():
    doc, attempt, event = new_document()
    lease = repository.claim_attempt_atomically(attempt.id, 'setup')
    version = repository.save_extraction_version(attempt.id, lease, None, AISuggestions(), ValidationReport(is_valid=False, status=DocumentStatus.REQUIRES_REVIEW))
    repository.approve_document_version(doc.id, version.id, 'auditor')
    return doc, version

def test_postgres_schema_and_reject_export_race():
    with repository.get_connection() as conn:
        cur = conn.cursor()
        cur.execute('SELECT current_database()')
        assert cur.fetchone()[0] == 'karik_audit_test'
    repository._init_schema()
    repository._init_schema()
    for _ in range(5):
        doc, version = approved_document()
        key = uuid.uuid4().hex
        barrier = threading.Barrier(2)
        def reject():
            barrier.wait()
            try:
                repository.reject_document(doc.id, 'auditor', 'race')
                return 'rejected'
            except ERPExportInFlightError:
                return 'blocked'
        def reserve():
            barrier.wait()
            try:
                repository.reserve_erp_export_atomic(doc.id, version.id, key, 'comarch_optima')
                return 'reserved'
            except DocumentNotApprovedError:
                return 'blocked'
        with ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(reject), pool.submit(reserve)
            result = (a.result(timeout=10), b.result(timeout=10))
        assert result in [('rejected', 'blocked'), ('blocked', 'reserved')]

def test_postgres_failed_export_concurrent_retry():
    doc, version = approved_document()
    key = uuid.uuid4().hex
    export_id, _, _ = repository.reserve_erp_export_atomic(doc.id, version.id, key, 'comarch_optima')
    repository.fail_erp_export(export_id, 'known failure before transmission')
    barrier = threading.Barrier(6)
    def reserve():
        barrier.wait()
        try:
            repository.reserve_erp_export_atomic(doc.id, version.id, key, 'comarch_optima')
            return True
        except ERPExportInFlightError:
            return False
    with ThreadPoolExecutor(6) as pool:
        assert sum(pool.map(lambda _: reserve(), range(6))) == 1

def test_postgres_delivery_backoff_and_limit():
    doc, attempt, event = new_document()
    repository.mark_outbox_event_sent(event.id)
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    with repository.get_connection() as conn:
        conn.cursor().execute('UPDATE processing_attempts SET created_at=%s WHERE id=%s', (old, attempt.id))
    assert attempt.id not in repository.recover_stuck_attempts()
    with repository.get_connection() as conn:
        conn.cursor().execute('UPDATE outbox_events SET sent_at=%s WHERE id=%s', (old, event.id))
    assert attempt.id in repository.recover_stuck_attempts()
    assert repository.get_outbox_event(event.id).status == 'RETRY'
    assert event.id not in [e.id for e in repository.get_pending_outbox_events()]
    assert attempt.id not in repository.recover_stuck_attempts()
    repository.mark_outbox_event_sent(event.id)
    assert attempt.id not in repository.recover_stuck_attempts()
    with repository.get_connection() as conn:
        conn.cursor().execute('UPDATE outbox_events SET sent_at=%s,retry_count=max_retries-1 WHERE id=%s', (old,event.id))
    repository.recover_stuck_attempts()
    assert repository.get_attempt(attempt.id).status.value == 'FAILED'
    assert repository.get_outbox_event(event.id).status == 'FAILED'

def test_http_redis_worker_crash_restart(tmp_path):
    # Reuse the existing parser fixture without importing unit-test conftest.
    tree = ast.parse((ROOT/'tests/test_ksef_and_decimal_math.py').read_text(encoding='utf-8'))
    test = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'test_ksef_xml_parser_fa2_valid')
    xml = next(ast.literal_eval(n.value) for n in test.body if isinstance(n, ast.Assign) and n.targets[0].id == 'xml_sample')
    xml = xml.replace(b'FV/2026/03/001', uuid.uuid4().hex.encode())
    gate = tmp_path/'gate'
    gate.touch()
    env = dict(os.environ, KARIK_TEST_WORKER_GATE=str(gate))
    processes, logs = [], []
    def start(args, name, child_env):
        log = (tmp_path/name).open('w', encoding='utf-8'); logs.append(log)
        p = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=child_env, stdout=log, stderr=subprocess.STDOUT)
        processes.append(p)
        return p
    headers={'Authorization': 'Bearer '+create_access_token('auditor',['integration'], {'integration':'auditor'}), 'X-Tenant-ID':'integration'}
    base='http://127.0.0.1:28001'
    try:
        start(['-m','uvicorn','main:app','--host','127.0.0.1','--port','28001'], 'api.log', os.environ.copy())
        worker=start(['integration_tests/worker.py'], 'worker1.log', env)
        def ready():
            try: return requests.get(base+'/openapi.json',timeout=1).status_code==200
            except requests.RequestException: return False
        eventually(ready)
        response=requests.post(base+'/v1/invoice/process',headers=headers,files={'file':('invoice.xml',xml,'application/xml')},timeout=20)
        assert response.status_code==202, response.text
        data=response.json(); att=data['attempt_id']; doc=data['document_id']
        eventually(lambda: Path(str(gate)+'.entered').exists())
        assert repository.get_attempt(att).status.value=='RUNNING'
        worker.kill(); worker.wait(timeout=10)
        # Advance only the dead worker's lease; no global database reset.
        with repository.get_connection() as conn:
            conn.cursor().execute('UPDATE processing_attempts SET heartbeat_at=%s WHERE id=%s', (datetime.now(timezone.utc)-timedelta(minutes=5),att))
        gate.unlink()
        start(['integration_tests/worker.py'], 'worker2.log', os.environ.copy())
        eventually(lambda: repository.get_attempt(att).status.value=='COMPLETED')
        result=requests.get(base+'/v1/invoice/status/'+doc,headers=headers,timeout=5)
        assert result.status_code==200
        assert result.json()['versions_count']==1
        assert result.json()['status']=='VALIDATED'
        # A missing source must terminate, not enter endless sweeper retries.
        bad_doc,bad_att,_=new_document()
        eventually(lambda: repository.get_attempt(bad_att.id).status.value=='FAILED')
        assert bad_att.id not in repository.recover_stuck_attempts(timeout_seconds=0)
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=10)
                except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=5)
        for log in logs: log.close()

