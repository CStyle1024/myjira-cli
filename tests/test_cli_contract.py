import json
import os
import tempfile
import unittest
from unittest.mock import patch

import yaml

from myjira_cli.cli import main
from myjira_cli.common import CliError, load_config, result, write_json_atomic
from tests.helpers import invoke, Opener


class CliContractTests(unittest.TestCase):
    def test_json_error_stdout_is_parseable(self):
        code, out, err = invoke(main, ['--format', 'json', 'nonexistent'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['status'], 'FAILED')
        self.assertEqual(json.loads(out)['schema_version'], 1)
        self.assertNotIn('Traceback', out + err)

    def test_yaml_preserves_full_doctor_response(self):
        response = result('SUCCESS', {'identity': {'name': 'cong.sui',
                                                  'displayName': '隋聪',
                                                  'active': True,
                                                  'groups': {'size': 0, 'items': []}},
                                      'write_permissions': 'not_checked'})
        with patch('myjira_cli.cli.load_config', return_value={}), patch(
                'myjira_cli.cli.dispatch', return_value=response):
            json_code, json_out, _ = invoke(main, ['doctor'])
            yaml_code, yaml_out, _ = invoke(main, ['doctor', '--format', 'yaml'])
        self.assertEqual((json_code, yaml_code), (0, 0))
        self.assertTrue(yaml_out.startswith('schema_version: 1\n'))
        self.assertEqual(yaml.safe_load(yaml_out), json.loads(json_out))
        self.assertEqual(yaml.safe_load(yaml_out), response)

    def test_yaml_reports_runtime_errors(self):
        with patch('myjira_cli.cli.load_config', side_effect=CliError('INPUT', 'Bad config.')):
            code, output, _ = invoke(main, ['doctor', '--format', 'yaml'])
        self.assertEqual(code, 2)
        self.assertEqual(yaml.safe_load(output), result(
            'FAILED', error={'code': 'INPUT', 'message': 'Bad config.', 'details': {}}))

    def test_credentials_are_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'project.json')
            write_json_atomic(p, {'jira': {'base_url': 'https://jira.test', 'auth': {'type': 'bearer', 'token_env': 'JIRA_TEST_SECRET'}}})
            with patch.dict(os.environ, {'JIRA_TEST_SECRET': 'never-print-me'}), patch('myjira_cli.http.build_opener', return_value=Opener(error=RuntimeError('never-print-me'))):
                code, out, err = invoke(main, ['--config', p, 'doctor'])
            self.assertNotEqual(code, 0)
            self.assertNotIn('never-print-me', out + err)

    def test_plaintext_credentials_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'project.json')
            write_json_atomic(p, {'jira': {'base_url': 'https://jira.test', 'auth': {'type': 'bearer', 'token': 'secret'}}})
            with self.assertRaises(CliError):
                load_config(p)

    def test_atomic_json_has_private_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'receipt.json')
            self.assertEqual(len(write_json_atomic(p, {'a': 1})), 64)
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
            with open(p) as f:
                self.assertEqual(json.load(f), {'a': 1})
