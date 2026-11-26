"""Standalone Jira CLI; JSON stdout, diagnostic stderr."""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .common import ToolInfoAction, CliError, EXIT_CODES, Parser, load_config, result
from .http import Client
from .jira import Jira, DEFAULT_JQL


def parser():
    p = Parser(prog='myjira-cli', allow_abbrev=False)
    p.add_argument('--version', action='version', version='myjira-cli ' + __version__)
    p.add_argument('--tool-info', action=ToolInfoAction, nargs=0, const=json.dumps({'schema_version': 1, 'distribution': 'myjira-cli', 'command': 'myjira-cli', 'version': __version__, 'result_schema': 1}))
    p.set_defaults(config=os.environ.get('ISSUE_RESOLVER_CONFIG', 'project.json'), format='json', timeout=30, state_dir=None)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='Read-only configuration and authentication probe')
    sub.add_parser('me', help='Current Jira identity')
    issues = sub.add_parser('issues').add_subparsers(dest='verb', required=True)
    listing = issues.add_parser('list')
    selection = listing.add_mutually_exclusive_group()
    selection.add_argument('--jql')
    selection.add_argument('--assignee', choices=('me',), default='me')
    listing.add_argument('--fields', default='summary,status,assignee,priority,updated')
    issue = sub.add_parser('issue').add_subparsers(dest='verb', required=True)
    for name in ('get', 'comments', 'transitions', 'changelog'):
        issue.add_parser(name).add_argument('key')
    fetch = issue.add_parser('fetch')
    fetch.add_argument('key')
    fetch.add_argument('--output-dir', required=True)
    for name in ('transition', 'comment', 'assign'):
        write = issue.add_parser(name)
        write.add_argument('key')
        mode = write.add_mutually_exclusive_group()
        mode.add_argument('--preview', action='store_true', help='Read-only preview (default)')
        mode.add_argument('--execute', action='store_true', help='Perform the write explicitly')
        write.add_argument('--operation-id')
        write.add_argument('--expected-preview-digest')
        if name == 'transition':
            write.add_argument('--transition-id', required=True)
            write.add_argument('--fields-file', help='JSON object of transition field values')
        elif name == 'comment':
            write.add_argument('--body-file', required=True, help='UTF-8 text file or - for stdin')
        else:
            write.add_argument('--user', required=True, help='Jira Server username, not Cloud accountId')
    attachment = sub.add_parser('attachment').add_subparsers(dest='verb', required=True)
    download = attachment.add_parser('download')
    download.add_argument('id')
    download.add_argument('--output-dir', required=True)
    return p


def dispatch(args, config):
    if 'jira' not in config:
        raise CliError('INPUT', 'Configuration requires a jira section.')
    cfg = config['jira']
    client = Client(cfg['base_url'], cfg.get('auth', {'type': 'none'}), args.timeout)
    jira = Jira(client)
    if args.command == 'doctor':
        return result('SUCCESS', {'identity': jira.me(), 'write_permissions': 'not_checked'})
    if args.command == 'me':
        return result('SUCCESS', jira.me())
    if args.command == 'issues':
        return result('SUCCESS', jira.search(args.jql or cfg.get('default_jql', DEFAULT_JQL), args.fields.split(',')))
    if args.command == 'issue':
        if args.verb in ('transition', 'comment', 'assign'):
            if args.verb == 'transition':
                values = {}
                if args.fields_file:
                    try:
                        values = json.loads(Path(args.fields_file).read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        raise CliError('INPUT', 'Cannot read transition field JSON.')
                fields = {'transition_id': args.transition_id, 'fields': values}
            elif args.verb == 'comment':
                try:
                    body = sys.stdin.read() if args.body_file == '-' else Path(args.body_file).read_text(encoding='utf-8')
                except (OSError, UnicodeError):
                    raise CliError('INPUT', 'Cannot read UTF-8 comment text.')
                fields = {'body': body}
            else:
                fields = {'username': args.user}
            if not args.execute:
                return result('SUCCESS', dict(jira.preview_write(args.verb, args.key, fields), mode='preview',
                                               correlation_marker_added=args.verb == 'comment'))
            default_state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))) / 'issue-resolver' / 'actions' / 'jira'
            state_dir = args.state_dir or cfg.get('state_dir') or config.get('state_dir') or str(default_state)
            return jira.execute_write(args.verb, args.key, fields, args.operation_id, state_dir, args.expected_preview_digest)
        if args.verb == 'fetch':
            data = jira.fetch(args.key, args.output_dir)
            return result('FAILED' if data['failures'] else 'SUCCESS', data)
        operation = {'get': jira.get_issue, 'comments': jira.comments, 'transitions': jira.transitions, 'changelog': jira.changelog}[args.verb]
        return result('SUCCESS', operation(args.key))
    if args.command == 'attachment':
        if not args.id.isdigit():
            raise CliError('INPUT', 'Attachment ID must be numeric.')
        metadata = client.request('GET', '/rest/api/2/attachment/' + args.id)
        return result('SUCCESS', jira.download_attachment(metadata, args.output_dir))
    raise CliError('INPUT', 'Unsupported Jira command.')


def main(argv: Optional[List[str]] = None) -> int:
    args = None
    try:
        args = parser().parse_args(argv)
        if args.command == 'issue' and args.verb in ('transition', 'comment', 'assign') and args.execute and not args.operation_id:
            raise CliError('INPUT', '--execute requires --operation-id for result reconciliation.')
        output = dispatch(args, load_config(args.config))
        code = 0 if output['status'] == 'SUCCESS' else (6 if output['status'] == 'UNKNOWN' else EXIT_CODES.get((output.get('error') or {}).get('code'), 5))
    except CliError as exc:
        code = EXIT_CODES.get(exc.code, 7)
        output = result('UNKNOWN' if code == 6 else 'FAILED', error=exc.as_dict())
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        code, output = 7, result('FAILED', error={'code': 'LOCAL_ERROR', 'message': 'Interrupted; inspect action receipt before retrying writes.'})
    except Exception:
        # Never echo exceptions from credentials, remote content or local config.
        code, output = 7, result('FAILED', error={'code': 'LOCAL_ERROR', 'message': 'Local operation failed. Check configuration, input and file permissions.'})
    print(json.dumps(output, ensure_ascii=False, indent=2 if args and args.format == 'text' else None))
    return code


if __name__ == '__main__':
    sys.exit(main())
