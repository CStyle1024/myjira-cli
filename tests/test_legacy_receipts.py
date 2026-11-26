import json, shutil, tempfile, unittest
from pathlib import Path
from myjira_cli.actions import run_action
from myjira_cli.common import digest, result

class LegacyReceiptTests(unittest.TestCase):
    def test_pre_split_receipts_never_repeat_execute(self):
        for state in ('confirmed', 'unknown'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as d:
                operation = 'legacy-' + state
                shutil.copyfile(Path(__file__).parent / 'fixtures' / (state + '.json'), Path(d) / (digest(operation) + '.json'))
                reconciled = []
                def reconcile():
                    reconciled.append(True)
                    return result('SUCCESS', {'id':'legacy'})
                value = run_action(d, operation, {'service':'https://fixture.invalid','key':'BUG-1'}, lambda: self.fail('must not write again'), reconcile)
                self.assertEqual(value['status'], 'SUCCESS')
                self.assertEqual(len(reconciled), 0 if state == 'confirmed' else 1)
