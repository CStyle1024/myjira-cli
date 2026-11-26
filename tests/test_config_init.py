import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from myjira_cli.cli import main


class ProbeServer(BaseHTTPRequestHandler):
    paths = {'/rest/api/2/myself': {'name': 'tester'}, '/rest/api/2/serverInfo': {'deploymentType': 'Server', 'version': '8.20.11'}}

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer sample-secret':
            self.send_error(401)
            return
        value = self.paths.get(self.path)
        if value is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())


class ConfigInitTests(unittest.TestCase):
    def test_verified_init_with_environment_reference(self):
        self.check_init(use_environment=True)

    def test_verified_init_with_hidden_pat(self):
        self.check_init(use_environment=False)

    def check_init(self, use_environment):
        with tempfile.TemporaryDirectory() as tmp, HTTPServer(('127.0.0.1', 0), ProbeServer) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = 'http://127.0.0.1:' + str(server.server_port)
                path = Path(tmp) / 'myjira-cli' / 'config.json'

                def initialize(token):
                    suffix = 'bearer\nJIRA_TOKEN\n' if use_environment else ''
                    answers = io.StringIO(url + '\n' + suffix)
                    answers.isatty = lambda: True
                    output, errors = io.StringIO(), io.StringIO()
                    argv = ['config', 'init'] + (['--auth-env'] if use_environment else [])
                    with patch('sys.stdin', answers), patch('getpass.getpass', return_value=token) as prompt:
                        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                            status = main(argv)
                    self.assertEqual(prompt.call_count, 0 if use_environment else 1)
                    self.assertNotIn(token, output.getvalue() + errors.getvalue())
                    return status, json.loads(output.getvalue())

                with patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp, 'JIRA_TOKEN': 'wrong',
                                             'ISSUE_RESOLVER_CONFIG': ''}):
                    status, failed = initialize('wrong')
                    self.assertNotEqual(status, 0, failed)
                    self.assertFalse(path.exists())
                    self.assertEqual(list(path.parent.glob('jira-pat-*')), [])
                    os.environ['JIRA_TOKEN'] = 'sample-secret'
                    status, initialized = initialize('sample-secret')
                    self.assertEqual(status, 0, initialized)
                    saved = json.loads(path.read_text())
                    self.assertNotIn('sample-secret', path.read_text())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    if not use_environment:
                        token_path = Path(saved['jira']['auth']['token_file'])
                        self.assertEqual(token_path.read_text(), 'sample-secret')
                        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
                        del os.environ['JIRA_TOKEN']
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main(['doctor']), 0, output.getvalue())
                    self.assertEqual(json.loads(output.getvalue())['data']['identity']['name'], 'tester')
                    self.assertEqual(saved['jira']['deployment_type'], 'Server')
            finally:
                server.shutdown()
                worker.join()
