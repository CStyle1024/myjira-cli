import json, shutil, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from myjira_cli.common import digest, write_json_atomic
from myjira_cli.jira import Jira
from myjira_cli.cli import main
from tests.helpers import invoke
from tests.test_jira_write import WriteClient

class DomainLegacyTests(unittest.TestCase):
    def test_old_comment_receipts_through_cli_never_post(self):
        fixture = Path(__file__).parent / 'fixtures'
        for status in ('confirmed', 'unknown'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as d:
                op = 'legacy-jira-' + status
                shutil.copyfile(fixture / ('jira-' + status + '.json'), Path(d) / (digest(op) + '.json'))
                client = WriteClient()
                client.comment_list = json.loads((fixture / ('jira-' + status + '-comments.json')).read_text())
                config, body = str(Path(d) / 'config.json'), str(Path(d) / 'body.txt')
                write_json_atomic(config, {'jira': {'base_url':client.base_url},'state_dir':d})
                Path(body).write_text('evidence')
                with patch('myjira_cli.cli.Client', return_value=client):
                    code, out, err = invoke(main, ['--config',config,'issue','comment','BUG-1','--body-file',body,'--execute','--operation-id',op])
                self.assertEqual(code, 0, out)
                self.assertEqual(json.loads(out)['status'], 'SUCCESS')
                self.assertTrue(all(call[0]=='GET' for call in client.calls))
