import socket
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from myjira_cli.common import CliError
from myjira_cli.http import Client, SafeRedirect
from tests.helpers import Response, Opener


class HttpTests(unittest.TestCase):
    def test_remote_http_requires_explicit_origin_permission(self):
        with self.assertRaises(CliError):
            Client('http://review.test', {'type': 'none'}, 1)
        client = Client('http://review.test', {'type': 'none'}, 1, transport={'allow_http': True})
        client.opener = Opener(Response(b'[]'))
        self.assertEqual(client.request('GET', '/changes/'), [])
        for url in ('http://review.test:81/file', 'http://other.test/file'):
            with self.assertRaises(CliError):
                client._open('GET', url)
        self.assertEqual(len(client.opener.requests), 1)

    def test_http_credentials_need_separate_permission_before_secret_read(self):
        with patch('myjira_cli.http.secret') as secret:
            with self.assertRaises(CliError):
                Client('http://review.test', {'type': 'basic', 'username': 'me', 'password_env': 'TEST'}, 1,
                       transport={'allow_http': True})
            secret.assert_not_called()
            secret.return_value = 'test-password'
            client = Client('http://review.test', {'type': 'basic', 'username': 'me', 'password_env': 'TEST'}, 1,
                            transport={'allow_http': True, 'allow_http_credentials': True})
            client.opener = Opener(Response(b'[]'))
            client.request('GET', '/a/changes/')
            self.assertTrue(client.opener.requests[0].get_header('Authorization').startswith('Basic '))

    def test_transport_boolean_types_and_dependencies(self):
        for config in ({'allow_http': 'false'}, {'allow_http': 1}, {'allow_http_credentials': True},
                       {'allow_http': True, 'allow_http_credentials': 'true'}, {'unknown': True}, [], 'http'):
            with self.subTest(config=config), self.assertRaises(CliError):
                Client('https://review.test', {'type': 'none'}, 1, transport=config)

    def test_http_redirect_permission_does_not_spread(self):
        handler = SafeRedirect(allowed_http_origin=('http', 'review.test', 80))
        request = Request('http://review.test/start', headers={'Authorization': 'Basic secret'})
        same = handler.redirect_request(request, None, 302, '', {}, 'http://review.test/end')
        self.assertEqual(same.get_header('Authorization'), 'Basic secret')
        for url in ('http://other.test/end', 'http://review.test:81/end'):
            with self.assertRaises(CliError):
                handler.redirect_request(request, None, 302, '', {}, url)
        secure = handler.redirect_request(request, None, 302, '', {}, 'https://files.test/end')
        self.assertIsNone(secure.get_header('Authorization'))
        with self.assertRaises(CliError):
            handler.redirect_request(secure, None, 302, '', {}, 'http://review.test/end')
        with self.assertRaises(CliError):
            handler.redirect_request(Request('http://review.test/a', data=b'{}', method='POST'),
                                     None, 307, '', {}, 'http://review.test/b')

    def client(self, response=None, error=None):
        c = Client('https://jira.test', {'type': 'none'}, 1)
        c.opener = Opener(response, error)
        return c

    def test_foreign_redirect_drops_auth(self):
        handler = SafeRedirect()
        request = Request('https://jira.test/file', headers={'Authorization': 'Bearer secret', 'Cookie': 'secret-cookie'})
        redirect = handler.redirect_request(request, None, 302, 'Found', {}, 'https://files.test/file')
        self.assertNotIn('Authorization', redirect.headers)
        self.assertNotIn('Cookie', redirect.headers)
        back = handler.redirect_request(redirect, None, 302, 'Found', {}, 'https://jira.test/final')
        self.assertNotIn('Authorization', back.headers)

    def test_https_downgrade_blocked(self):
        with self.assertRaises(CliError):
            SafeRedirect().redirect_request(Request('https://jira.test/a'), None, 302, '', {}, 'http://jira.test/a')

    def test_http_auth_errors_are_classified(self):
        for status in (401, 403):
            c = self.client(error=HTTPError('https://jira.test', status, 'secret error', {}, None))
            with self.assertRaises(CliError) as caught:
                c.request('GET', '/rest/api/2/myself')
            self.assertEqual(caught.exception.code, 'AUTH_REQUIRED')

    def test_timeout_and_tls_failures_are_not_success(self):
        for error in (socket.timeout(), URLError(ssl.SSLError('certificate'))):
            c = self.client(error=error)
            with self.assertRaises(CliError):
                c.request('GET', '/rest/api/2/myself')

    def test_login_html_is_auth_error(self):
        c = self.client(Response(b'<html>login</html>', content_type='text/html'))
        with self.assertRaises(CliError) as caught:
            c.request('GET', '/rest/api/2/myself')
        self.assertEqual(caught.exception.code, 'AUTH_REQUIRED')

    def test_mutating_redirect_is_not_replayed(self):
        with self.assertRaises(CliError):
            SafeRedirect().redirect_request(Request('https://jira.test/a', data=b'{}', method='POST'), None, 307, '', {}, 'https://jira.test/b')

    def test_external_request_is_rejected(self):
        c = self.client()
        with self.assertRaises(CliError):
            c.request('GET', 'https://elsewhere.test/a')

    def test_content_length_mismatch_is_failure(self):
        c = self.client(Response(b'part', content_type='application/octet-stream', headers={'Content-Length': '20'}))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(CliError):
                c.download('https://jira.test/file', d + '/file')
