"""Opt-in integration suite. Never connects to the application's database."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if os.environ.get('KARIK_RUN_INTEGRATION') != '1':
    raise RuntimeError('Use the isolated services and KARIK_RUN_INTEGRATION=1; see integration_tests/README.md')

os.environ.update({
    'KARIK_DB_ENGINE': 'postgres',
    'DATABASE_URL': 'postgresql://audit:audit-local-test@127.0.0.1:25432/karik_audit_test',
    'POSTGRES_DB': 'karik_audit_test',
    'REDIS_URL': 'redis://127.0.0.1:26379/0',
    'KARIK_AUTH_SECRET': 'isolated-integration-secret-not-for-production-2026',
    'KARIK_ENVIRONMENT': 'test',
    'KARIK_ENABLE_BACKGROUND_DISPATCHER': '1',
    'ERP_TEST_MODE': 'false',
    'GEMINI_API_KEY': '',
    'STORAGE_BASE_DIR': str(ROOT / '.cache' / 'integration_documents'),
})

