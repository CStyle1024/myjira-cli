"""交互式初始化独立连接配置。"""
import getpass
import os
import re
import sys
import tempfile
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from .common import CliError, file_lock, load_config, result, write_json_atomic
from .http import Client

TOOL = 'myjira-cli'


def standalone_config():
    root = os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config'
    return Path(root) / TOOL / 'config.json'


def config_path(explicit):
    # 显式工作流配置优先。尚未初始化时兼容原有 project.json 默认值。
    configured = explicit or os.environ.get('ISSUE_RESOLVER_CONFIG')
    if configured:
        return configured
    path = standalone_config()
    return str(path) if path.exists() else 'project.json'


def ask(label):
    print(label + ': ', end='', file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:
        raise CliError('INPUT', 'Initialization ended before all answers were provided.')
    return line.strip()


def env_ref(label):
    name = ask(label)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        raise CliError('INPUT', 'Enter an environment variable name.')
    return name


def auth_config():
    mode = ask('Authentication (bearer/basic)')
    if mode == 'bearer':
        return {'type': mode, 'token_env': env_ref('Token environment variable')}
    if mode == 'basic':
        return {'type': mode, 'username_env': env_ref('Username environment variable'),
                'password_env': env_ref('Password or API token environment variable')}
    raise CliError('INPUT', 'Authentication must be bearer or basic.')


def read_pat():
    # getpass 无法关闭终端回显时会警告并回退，必须在读取前中止。
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return getpass.getpass('Jira PAT (hidden): ').strip()
    except (getpass.GetPassWarning, EOFError, OSError):
        raise CliError('INPUT', 'Cannot read a hidden PAT from this terminal.')


def save_pat(directory, token):
    fd, filename = tempfile.mkstemp(prefix='jira-pat-', dir=str(directory))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(filename).unlink()
        raise
    return Path(filename)


def add_commands(commands):
    sub = commands.add_parser('config', help='Manage standalone connection configuration')
    init = sub.add_subparsers(dest='config_command', required=True).add_parser(
        'init', help='Verify a connection and save credentials by reference')
    init.add_argument('--auth-env', action='store_true',
                      help='Use environment variables instead of hidden PAT input')


def initialize(args):
    if not sys.stdin.isatty():
        raise CliError('INPUT', 'Run config init from an interactive terminal.')
    if args.config is not None:
        raise CliError('INPUT', 'config init saves to the standalone config and does not take --config.')
    path = standalone_config()
    if not path.is_absolute():
        raise CliError('INPUT', 'XDG_CONFIG_HOME must be an absolute path.')
    url = ask('Service base URL')
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise CliError('INPUT', 'Invalid service URL.')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise CliError('INPUT', 'Enter an HTTP(S) REST base URL.')
    token = None if args.auth_env else read_pat()
    auth = auth_config() if args.auth_env else {'type': 'bearer'}
    section = {'base_url': url.rstrip('/'), 'auth': auth}
    metadata, server = probe(args, section, token)
    with file_lock(str(path) + '.lock'):
        config = load_config(str(path)) if path.exists() else {}
        target = target_section(config, server)
        if target[1] in target[0]:
            if ask('Update this existing connection? Type yes to confirm') != 'yes':
                raise CliError('CONFLICT', 'Existing configuration was left unchanged.')
            previous = target[0][target[1]]
            section = {**previous, **section}
            # 重新初始化时以这次验证的传输策略为准。
            if parsed.scheme != 'http':
                section.pop('transport', None)
        saved_pat = save_pat(path.parent, token) if token is not None else None
        if saved_pat is not None:
            section['auth']['token_file'] = str(saved_pat)
        target[0][target[1]] = section
        try:
            write_json_atomic(str(path), config)
        except BaseException:
            # 原子替换有可能已经发生，只在确认配置未引用新文件时清理。
            if saved_pat is not None:
                current = load_config(str(path)) if path.exists() else {}
                if current.get('jira', {}).get('auth', {}).get('token_file') != str(saved_pat):
                    saved_pat.unlink()
            raise
    return result('SUCCESS', {'config': str(path), 'server': server, **metadata})


def probe(args, section, token):
    from .jira import Jira
    client = Client(section['base_url'], section['auth'], args.timeout, bearer_token=token)
    identity = Jira(client).me()
    if not any(isinstance(identity.get(key), str) and identity[key]
               for key in ('accountId', 'key', 'name')):
        raise CliError('AUTH_REQUIRED', 'Jira did not return an identifiable current account.')
    try:
        info = client.request('GET', '/rest/api/2/serverInfo')
    except CliError:
        info = None
    info = info if isinstance(info, dict) else {}
    detected = info.get('deploymentType')
    deployment = {'cloud': 'Cloud', 'server': 'Server', 'datacenter': 'DataCenter',
                  'data center': 'DataCenter'}.get(str(detected).casefold())
    if deployment is None:
        choices = {'cloud': 'Cloud', 'server': 'Server', 'datacenter': 'DataCenter'}
        choice = ask('Deployment type unavailable (cloud/server/datacenter/unknown)').lower()
        if choice not in choices and choice != 'unknown':
            raise CliError('INPUT', 'Unknown deployment type choice.')
        deployment = choices.get(choice)
    if deployment is not None:
        section['deployment_type'] = deployment
    else:
        section['deployment_type'] = None
    return {'deployment_type': deployment, 'version': info.get('version'),
            'write_permissions': 'not_checked'}, None


def target_section(config, server):
    return config, 'jira'
