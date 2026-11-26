"""Small shared CLI and file utilities; no workflow-specific state."""
import hashlib
import fcntl
import argparse
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional


EXIT_CODES = {'INPUT': 2, 'AUTH_REQUIRED': 3, 'CONFLICT': 4, 'PRECONDITION': 4,
              'REMOTE_ERROR': 5, 'RESULT_UNKNOWN': 6, 'LOCAL_ERROR': 7}


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('allow_abbrev', False)
        super().__init__(*args, **kwargs)
        self.add_argument('--config', default=argparse.SUPPRESS, help='Project JSON file (or ISSUE_RESOLVER_CONFIG)')
        self.add_argument('--format', choices=('json', 'text', 'yaml'), default=argparse.SUPPRESS)
        self.add_argument('--timeout', type=float, default=argparse.SUPPRESS)
        self.add_argument('--state-dir', default=argparse.SUPPRESS, help='Local operation records')

    def error(self, message):
        raise CliError('INPUT', 'Invalid command arguments. Use --help for supported syntax.')


class CliError(Exception):
    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self):
        return {'code': self.code, 'message': self.message, 'details': self.details}


def result(status: str, data: Any = None, error: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
    return dict(schema_version=1, status=status, data=data, error=error, **extra)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json_atomic(path: str, value: Any) -> str:
    return write_bytes_atomic(path, canonical(value))


def write_bytes_atomic(path: str, data: bytes) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.pending-', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, str(target))
        parent = os.open(str(target.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return hashlib.sha256(data).hexdigest()


@contextmanager
def file_lock(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CliError('CONFLICT', 'Another process owns this operation or archive.')
        yield
    finally:
        os.close(fd)


def load_config(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding='utf-8') as f:
            config = json.load(f)
    except (OSError, ValueError):
        raise CliError('INPUT', 'Cannot read a valid JSON configuration file.')
    if not isinstance(config, dict):
        raise CliError('INPUT', 'Configuration must be a JSON object.')
    sections = [(name, config[name]) for name in ('jira', 'gerrit') if name in config]
    if 'gerrits' in config:
        if not isinstance(config['gerrits'], dict) or not config['gerrits']:
            raise CliError('INPUT', 'gerrits must be a non-empty mapping.')
        for name, section in config['gerrits'].items():
            if not isinstance(name, str) or not name.strip():
                raise CliError('INPUT', 'Gerrit server names must be non-empty strings.')
            sections.append(('gerrits.' + name, section))
    for service, section in sections:
        if not isinstance(section, dict) or not isinstance(section.get('base_url'), str):
            raise CliError('INPUT', service + '.base_url is required.')
        auth = section.get('auth', {'type': 'none'})
        allowed = {'type', 'token_env', 'token_file', 'username', 'username_env', 'password_env', 'password_file'}
        if not isinstance(auth, dict) or set(auth) - allowed:
            raise CliError('INPUT', 'Authentication accepts credential references only.')
    return config


def gerrit_config(config, server=None):
    if server is not None:
        servers = config.get('gerrits', {})
        if not isinstance(servers, dict) or server not in servers or not isinstance(servers[server], dict):
            raise CliError('INPUT', 'Unknown Gerrit server mapping.')
        return servers[server]
    if config.get('gerrits'):
        raise CliError('INPUT', 'Choose a Gerrit server explicitly.')
    section = config.get('gerrit')
    if not isinstance(section, dict) or not section:
        raise CliError('INPUT', 'Configuration requires a gerrit section.')
    return section


def secret(auth: dict, name: str) -> str:
    env_name, filename = auth.get(name + '_env'), auth.get(name + '_file')
    if bool(env_name) == bool(filename):
        raise CliError('INPUT', 'Configure exactly one credential environment or file reference.')
    try:
        value = os.environ.get(env_name, '') if env_name else Path(filename).expanduser().read_text(encoding='utf-8').strip()
    except (OSError, ValueError, TypeError):
        raise CliError('AUTH_REQUIRED', 'Cannot read the configured credential reference.')
    if not value or '\r' in value or '\n' in value:
        raise CliError('AUTH_REQUIRED', 'Configured credential is missing or invalid.')
    return value


class ToolInfoAction(argparse.Action):
    """Emit machine identity without argparse's help-text line wrapping."""
    def __call__(self, parser, namespace, values, option_string=None):
        print(self.const)
        parser.exit()
