"""Exercise real CLI processes and real loopback HTTP, not patched clients."""
import json
import errno
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from types import SimpleNamespace
from unittest.mock import patch

from myjira_cli.common import write_json_atomic
from myjira_cli.cli import main
from tests.helpers import invoke


class MockJira(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data=None, code=200, raw=None):
        content = raw if raw is not None else json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/octet-stream' if raw is not None else 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlsplit(self.path)
        p, q, state = parsed.path, parse_qs(parsed.query), self.server.state
        if p.endswith('/myself'):
            return self.reply({'name': 'engineer'})
        if p == '/rest/api/2/search':
            start = int(q.get('startAt', ['0'])[0])
            return self.reply({'startAt': start, 'maxResults': 1, 'total': 2, 'issues': [{'id': str(start), 'key': 'BUG-' + str(start + 1)}] if start < 2 else []})
        if p.endswith('/transitions'):
            return self.reply({'transitions': [{'id': '21', 'to': {'id': '3'}, 'fields': {}}]})
        if p.endswith('/user/assignable/search'):
            return self.reply([{'name': 'tester'}])
        if p.endswith('/comment'):
            start = int(q.get('startAt', ['0'])[0])
            return self.reply({'startAt': start, 'maxResults': 1, 'total': len(state['comments']), 'comments': state['comments'][start:start + 1]})
        if '/comment/' in p:
            return self.reply(state['comments'][int(p.rsplit('/', 1)[1]) - 1])
        if p == '/secure/attachment/1':
            return self.reply(raw=b'log')
        if p.endswith('/issue/BUG-1'):
            if q.get('expand') == ['changelog']:
                return self.reply({'changelog': {'startAt': 0, 'total': 0, 'histories': []}})
            return self.reply({'key': 'BUG-1', 'fields': {'summary': 'Mock bug', 'status': {'id': state['status']},
                               'assignee': {'name': state['assignee']}, 'attachment': [{'id': '1', 'filename': 'log.txt', 'size': 3,
                               'content': 'http://127.0.0.1:' + str(self.server.server_port) + '/secure/attachment/1'}]}})
        self.reply({'error': 'not found'}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        state = self.server.state
        state['writes'] += 1
        if self.path.endswith('/comment'):
            item = {'id': str(len(state['comments']) + 1), 'body': body['body'], 'author': {'name': 'engineer'}}
            state['comments'].append(item)
            if state['drop']:
                state['drop'] = False
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            return self.reply(item, 201)
        state['status'] = '3'
        self.reply(code=204, raw=b'')

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.state['assignee'] = body['name']
        self.server.state['writes'] += 1
        self.reply(code=204, raw=b'')


class IntegrationTests(unittest.TestCase):
    def test_cli_read_fetch_and_write_recovery_over_http(self):
        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), MockJira)
        except PermissionError as exc:
            if exc.errno == errno.EPERM:
                self.skipTest('Sandbox prohibits sockets; run on host for real loopback HTTP acceptance.')
            raise
        server.state = {'status': '1', 'assignee': 'engineer', 'comments': [], 'drop': True, 'writes': 0}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                config, body = str(Path(d) / 'project.json'), str(Path(d) / 'comment.txt')
                write_json_atomic(config, {'jira': {'base_url': 'http://127.0.0.1:' + str(server.server_port)}, 'state_dir': str(Path(d) / 'actions')})
                Path(body).write_text('验证结果', encoding='utf-8')

                def cli(args, expected=0):
                    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
                    p = subprocess.run([sys.executable, '-m', 'myjira_cli.cli', '--config', config] + args,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=10)
                    self.assertEqual(p.returncode, expected, p.stdout.decode() + p.stderr.decode())
                    self.assertNotIn(b'Traceback', p.stdout + p.stderr)
                    return json.loads(p.stdout)

                self.assertEqual(cli(['me'])['data']['name'], 'engineer')
                self.assertEqual(len(cli(['issues', 'list'])['data']), 2)
                archive = cli(['issue', 'fetch', 'BUG-1', '--output-dir', d + '/bug'])['data']
                self.assertEqual(Path(archive['artifacts'][0]['path']).read_bytes(), b'log')
                self.assertEqual(cli(['issue', 'assign', 'BUG-1', '--user', 'tester'])['data']['mode'], 'preview')
                self.assertEqual(server.state['writes'], 0)
                command = ['issue', 'comment', 'BUG-1', '--body-file', body, '--execute', '--operation-id', 'integration-comment']
                self.assertEqual(cli(command, 6)['status'], 'UNKNOWN')
                self.assertEqual(cli(command)['status'], 'SUCCESS')
                self.assertEqual(server.state['writes'], 1)
                cli(['issue', 'transition', 'BUG-1', '--transition-id', '21', '--execute', '--operation-id', 'integration-transition'])
                cli(['issue', 'assign', 'BUG-1', '--user', 'tester', '--execute', '--operation-id', 'integration-assign'])
                self.assertEqual(server.state['writes'], 3)
                self.assertEqual(server.state['status'], '3')
                self.assertEqual(server.state['assignee'], 'tester')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_serialized_http_without_network_sockets(self):
        server = SimpleNamespace(server_port=8123, state={'status': '1', 'assignee': 'engineer', 'comments': [], 'drop': True, 'writes': 0})

        class Peer:
            def __init__(self, data):
                self.input = io.BytesIO(data)
                self.output = bytearray()

            def makefile(self, *args):
                return self.input

            def sendall(self, data):
                self.output.extend(data)

            def shutdown(self, *args):
                pass

            def close(self):
                pass

        class WireSocket(Peer):
            def __init__(self):
                super().__init__(b'')

            def setsockopt(self, *args):
                pass

            def settimeout(self, *args):
                pass

            def makefile(self, *args):
                peer = Peer(bytes(self.output))
                MockJira(peer, ('127.0.0.1', 10000), server)
                return io.BytesIO(bytes(peer.output))

        with tempfile.TemporaryDirectory() as d, patch('socket.create_connection', side_effect=lambda *a, **k: WireSocket()):
            config, body = str(Path(d) / 'config.json'), str(Path(d) / 'body.txt')
            write_json_atomic(config, {'jira': {'base_url': 'http://127.0.0.1:8123'}, 'state_dir': d + '/actions'})
            Path(body).write_text('中文 evidence', encoding='utf-8')

            def cli(args, expected=0):
                code, out, err = invoke(main, ['--config', config] + args)
                self.assertEqual(code, expected, out + err)
                return json.loads(out)

            self.assertEqual(cli(['me'])['data']['name'], 'engineer')
            self.assertEqual(len(cli(['issues', 'list'])['data']), 2)
            archived = cli(['issue', 'fetch', 'BUG-1', '--output-dir', d + '/bug'])['data']
            self.assertEqual(Path(archived['artifacts'][0]['path']).read_bytes(), b'log')
            command = ['issue', 'comment', 'BUG-1', '--body-file', body, '--execute', '--operation-id', 'wire-comment']
            self.assertEqual(cli(command, 6)['status'], 'UNKNOWN')
            self.assertEqual(cli(command)['status'], 'SUCCESS')
            cli(['issue', 'transition', 'BUG-1', '--transition-id', '21', '--execute', '--operation-id', 'wire-transition'])
            cli(['issue', 'assign', 'BUG-1', '--user', 'tester', '--execute', '--operation-id', 'wire-assign'])
            self.assertEqual(server.state['writes'], 3)
            self.assertEqual(server.state['status'], '3')
            self.assertEqual(server.state['assignee'], 'tester')
