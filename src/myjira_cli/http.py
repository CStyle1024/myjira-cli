"""Conservative JSON transport with origin-bound credentials."""
import base64
import hashlib
import http.client
import json
import math
import os
import socket
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .common import CliError, canonical, secret


def origin(url):
    p = urlsplit(url)
    return p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == 'https' else 80)


def validate_url(url, allowed_http_origin=None):
    try:
        p = urlsplit(url)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError()
        origin(url)
        if (p.scheme == 'http' and p.hostname not in ('localhost', '127.0.0.1', '::1')
                and origin(url) != allowed_http_origin):
            raise ValueError()
    except (ValueError, TypeError):
        raise CliError('INPUT', 'Use HTTPS without embedded credentials; remote HTTP requires explicit origin permission.')


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, allowed_http_origin=None):
        super().__init__()
        self.allowed_http_origin = allowed_http_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.allowed_http_origin)
        if req.get_method() not in ('GET', 'HEAD'):
            raise CliError('RESULT_UNKNOWN', 'Write redirect was not replayed; reconcile the remote result.')
        if urlsplit(req.full_url).scheme == 'https' and urlsplit(newurl).scheme != 'https':
            raise CliError('AUTH_REQUIRED', 'HTTPS downgrade redirect refused.')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and origin(req.full_url) != origin(newurl):
            for key in ('Authorization', 'Cookie', 'Proxy-authorization'):
                redirected.remove_header(key)
        return redirected


class Client:
    def __init__(self, base_url: str, auth: dict, timeout: float, transport=None, *, bearer_token=None):
        transport = {} if transport is None else transport
        if (not isinstance(transport, dict) or set(transport) - {'allow_http', 'allow_http_credentials'}
                or any(type(value) is not bool for value in transport.values())):
            raise CliError('INPUT', 'Transport accepts only allow_http and allow_http_credentials booleans.')
        if transport.get('allow_http_credentials', False) and not transport.get('allow_http', False):
            raise CliError('INPUT', 'allow_http_credentials requires allow_http.')
        try:
            self.allowed_http_origin = origin(base_url) if transport.get('allow_http', False) else None
        except (ValueError, TypeError, AttributeError):
            raise CliError('INPUT', 'Invalid service base URL.')
        validate_url(base_url, self.allowed_http_origin)
        if urlsplit(base_url).query:
            raise CliError('INPUT', 'Base URL must not contain a query.')
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise CliError('INPUT', 'Timeout must be a positive finite number.')
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.opener = build_opener(SafeRedirect(self.allowed_http_origin))
        self.authorization = ''
        auth_type = auth.get('type', 'none')
        parsed = urlsplit(base_url)
        if (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1')
                and auth_type != 'none' and not transport.get('allow_http_credentials', False)):
            raise CliError('INPUT', 'Credentials over remote HTTP require allow_http_credentials=true; HTTP is unencrypted.')
        if auth_type == 'bearer':
            token = secret(auth, 'token') if bearer_token is None else bearer_token
            if not token.strip() or '\r' in token or '\n' in token:
                raise CliError('AUTH_REQUIRED', 'Configured credential is missing or invalid.')
            self.authorization = 'Bearer ' + token
        elif auth_type == 'basic':
            username = auth.get('username') or os.environ.get(auth.get('username_env', ''), '')
            if not isinstance(username, str) or not username or ':' in username or '\n' in username or '\r' in username:
                raise CliError('INPUT', 'Basic authentication needs a valid username.')
            raw = (username + ':' + secret(auth, 'password')).encode()
            self.authorization = 'Basic ' + base64.b64encode(raw).decode('ascii')
        elif auth_type != 'none':
            raise CliError('INPUT', 'Unsupported authentication type; use none, bearer or basic.')

    def _open(self, method, url, body=None, accept='application/json'):
        validate_url(url, self.allowed_http_origin)
        headers = {'Accept': accept, 'User-Agent': 'local-issue-resolver/0.1'}
        if origin(url) == origin(self.base_url) and self.authorization:
            headers['Authorization'] = self.authorization
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = canonical(body)
        req = Request(url, data=data, headers=headers, method=method)
        try:
            return self.opener.open(req, timeout=self.timeout)
        except HTTPError as exc:
            status = exc.code
            if exc.fp is not None:
                exc.close()
            if status in (401, 403):
                raise CliError('AUTH_REQUIRED', 'Service authentication or permission check failed.', {'http_status': status})
            if method not in ('GET', 'HEAD') and status >= 500:
                raise CliError('RESULT_UNKNOWN', 'Remote write result is unknown.', {'http_status': status})
            raise CliError('REMOTE_ERROR', 'Service rejected the request.', {'http_status': status})
        except (URLError, OSError, socket.timeout, http.client.HTTPException):
            code = 'REMOTE_ERROR' if method in ('GET', 'HEAD') else 'RESULT_UNKNOWN'
            raise CliError(code, 'Connection failed or timed out; no automatic write retry was attempted.')

    def request(self, method: str, path: str, query=None, body=None):
        if not path.startswith('/') or path.startswith('//') or urlsplit(path).scheme:
            raise CliError('INPUT', 'API path must be relative to the configured service.')
        url = self.base_url + path
        if query:
            url += '?' + urlencode(query, doseq=True)
        try:
            with self._open(method, url, body) as response:
                raw = response.read()
                if not raw:
                    return None
                if 'html' in response.headers.get('Content-Type', '').lower():
                    code = 'AUTH_REQUIRED' if method in ('GET', 'HEAD') else 'RESULT_UNKNOWN'
                    raise CliError(code, 'Service returned HTML instead of a verifiable JSON response.')
                try:
                    if raw.startswith(b")]}'\n"):
                        raw = raw[5:]
                    return json.loads(raw.decode('utf-8'))
                except (ValueError, UnicodeError):
                    code = 'REMOTE_ERROR' if method in ('GET', 'HEAD') else 'RESULT_UNKNOWN'
                    raise CliError(code, 'Service did not return valid JSON.')
        except (OSError, http.client.HTTPException):
            raise CliError('REMOTE_ERROR' if method in ('GET', 'HEAD') else 'RESULT_UNKNOWN', 'Response was interrupted.')

    def download(self, url: str, path: str) -> dict:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.download-', dir=str(target.parent))
        h, size = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, 'wb') as output, self._open('GET', url, accept='application/octet-stream') as response:
                if 'html' in response.headers.get('Content-Type', '').lower() and 'login' in response.geturl().lower():
                    raise CliError('AUTH_REQUIRED', 'Attachment request returned a login page.')
                length = response.headers.get('Content-Length')
                for block in iter(lambda: response.read(1024 * 1024), b''):
                    output.write(block)
                    size += len(block)
                    h.update(block)
                if length is not None and (not length.isdigit() or size != int(length)):
                    raise CliError('REMOTE_ERROR', 'Attachment size does not match its response length.')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, str(target))
            return {'path': str(target.resolve()), 'sha256': h.hexdigest(), 'size': size,
                    'source_url': url, 'final_url': response.geturl(),
                    'cross_origin': origin(url) != origin(response.geturl())}
        except (OSError, http.client.HTTPException):
            raise CliError('REMOTE_ERROR', 'Attachment download was interrupted.')
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
