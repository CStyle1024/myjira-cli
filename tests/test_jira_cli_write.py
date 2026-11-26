import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myjira_cli.common import write_json_atomic
from myjira_cli.jira import Jira
from myjira_cli.cli import main
from tests.helpers import invoke
from tests.test_jira_write import WriteClient


class WriteCliTests(unittest.TestCase):
    def test_write_defaults_to_preview_and_explicit_execution_works(self):
        with tempfile.TemporaryDirectory() as d:
            config = str(Path(d) / 'project.json')
            write_json_atomic(config, {'jira': {'base_url': 'https://jira.test', 'auth': {'type': 'none'}}, 'state_dir': d})
            c = WriteClient()
            with patch('myjira_cli.cli.Jira', return_value=Jira(c)):
                code, out, _ = invoke(main, ['--config', config, 'issue', 'assign', 'BUG-1', '--user', 'tester'])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)['data']['mode'], 'preview')
                self.assertEqual(c.assignee, 'me')
                code, out, _ = invoke(main, ['--config', config, 'issue', 'assign', 'BUG-1', '--user', 'tester', '--execute', '--operation-id', 'assign-1'])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)['status'], 'SUCCESS')
                self.assertEqual(c.assignee, 'tester')

    def test_execute_requires_operation_id_before_network(self):
        with patch('myjira_cli.cli.Client') as client:
            code, out, _ = invoke(main, ['issue', 'assign', 'BUG-1', '--user', 'tester', '--execute'])
            self.assertEqual(code, 2)
            self.assertIn('operation', json.loads(out)['error']['message'].lower())
            client.assert_not_called()

    def test_comment_body_file_and_unknown_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            config, body = str(Path(d) / 'p.json'), str(Path(d) / 'comment.txt')
            write_json_atomic(config, {'jira': {'base_url': 'https://jira.test'}, 'state_dir': d})
            Path(body).write_text('中文复现证据', encoding='utf-8')
            c = WriteClient()
            c.drop = True
            args = ['--config', config, 'issue', 'comment', 'BUG-1', '--body-file', body, '--execute', '--operation-id', 'comment-1']
            with patch('myjira_cli.cli.Jira', return_value=Jira(c)):
                code, out, _ = invoke(main, args)
                self.assertEqual(code, 6)
                self.assertEqual(json.loads(out)['status'], 'UNKNOWN')
                self.assertEqual(invoke(main, args)[0], 0)
                self.assertEqual(len(c.comment_list), 1)

    def test_global_options_supported_after_subcommands(self):
        with tempfile.TemporaryDirectory() as d:
            config = str(Path(d) / 'p.json')
            write_json_atomic(config, {'jira': {'base_url': 'https://jira.test'}})
            with patch('myjira_cli.cli.Jira', return_value=Jira(WriteClient())):
                code, out, _ = invoke(main, ['me', '--format', 'json', '--config', config])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)['data']['name'], 'me')

    def test_known_write_precondition_has_conflict_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            config = str(Path(d) / 'p.json')
            write_json_atomic(config, {'jira': {'base_url': 'https://jira.test'}, 'state_dir': d})
            c = WriteClient()
            c.required = True
            with patch('myjira_cli.cli.Jira', return_value=Jira(c)):
                code, out, _ = invoke(main, ['--config', config, 'issue', 'transition', 'BUG-1', '--transition-id', '21', '--execute', '--operation-id', 'missing-fields'])
                self.assertEqual(json.loads(out)['error']['code'], 'PRECONDITION')
                self.assertEqual(code, 4)
