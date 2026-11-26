"""Write a reviewable JSON test report; exits nonzero on any failure."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ReportResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append({'test': test.id(), 'status': 'passed'})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.cases.append({'test': test.id(), 'status': 'skipped', 'reason': reason})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.cases.append({'test': test.id(), 'status': 'failed'})

    def addError(self, test, err):
        super().addError(test, err)
        self.cases.append({'test': test.id(), 'status': 'error'})


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
    began = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=ReportResult).run(suite)
    report = {'executed_at_utc': datetime.now(timezone.utc).isoformat(), 'duration_seconds': round(time.monotonic() - began, 3),
        'total': result.testsRun, 'passed': len([c for c in result.cases if c['status'] == 'passed']),
        'failed': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped),
        'offline_harness': 'Actual ASGI decoder + SQL service; SQLite type/lock adaptations; fake external providers',
        'postgresql': 'Enabled only with EASYQ_TEST_DATABASE_URL naming a dedicated *_test database',
        'cases': result.cases}
    (ROOT / 'docs' / 'service').mkdir(parents=True, exist_ok=True)
    (ROOT / 'docs' / 'service' / 'test-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)
