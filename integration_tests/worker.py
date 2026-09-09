"""Real Celery worker; optional gate allows deterministic process-crash injection."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tasks

original = tasks._execute_invoice_pipeline
def gated(*args, **kwargs):
    gate = os.environ.get('KARIK_TEST_WORKER_GATE')
    if gate:
        Path(gate + '.entered').touch()
        while Path(gate).exists():
            time.sleep(0.1)
    return original(*args, **kwargs)

tasks._execute_invoice_pipeline = gated
tasks.celery_app.worker_main(['worker', '--pool=solo', '--concurrency=1', '--loglevel=INFO', '--without-mingle', '--without-gossip'])

