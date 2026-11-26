"""Jira Server/Data Center v2 operations, independent of bugfix workflows."""
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from urllib.parse import quote

from .common import CliError, digest, file_digest, file_lock, result, write_json_atomic, write_bytes_atomic
from .actions import run_action


API = '/rest/api/2'
DEFAULT_JQL = 'assignee = currentUser() AND issuetype = Bug AND statusCategory != Done ORDER BY priority DESC, updated DESC'


def checked_object(value):
    if not isinstance(value, dict):
        raise CliError('REMOTE_ERROR', 'Expected a JSON object from Jira.')
    return value


def issue_path(key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-[0-9]+|[0-9]+', key):
        raise CliError('INPUT', 'Use a Jira issue key (e.g. BUG-123) or numeric ID.')
    return API + '/issue/' + key


def archive_path(root, *parts):
    root = Path(root).resolve()
    target = root.joinpath(*parts)
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise CliError('INPUT', 'Archive path escapes the output directory.')
    return target


class Jira:
    def __init__(self, client):
        self.client = client

    def me(self) -> dict:
        return checked_object(self.client.request('GET', API + '/myself'))

    def _pages(self, path, field, query=None):
        items, seen, identifiers, start = [], set(), set(), 0
        for _ in range(10000):
            q = dict(query or {}, startAt=start, maxResults=100)
            response = checked_object(self.client.request('GET', path, q))
            page = response.get(field)
            if not isinstance(page, list) or any(not isinstance(x, dict) for x in page):
                raise CliError('REMOTE_ERROR', 'Unexpected paginated Jira response.')
            if not page:
                return items
            if response.get('startAt', start) != start:
                raise CliError('REMOTE_ERROR', 'Jira returned a different page offset.')
            marker = digest(page)
            if marker in seen:
                raise CliError('REMOTE_ERROR', 'Jira repeated a page; refusing an incomplete result.')
            seen.add(marker)
            for item in page:
                identity = item.get('id') or item.get('key')
                if not isinstance(identity, (str, int)):
                    raise CliError('REMOTE_ERROR', 'Paginated Jira item has no stable identity.')
                identity = str(identity)
                if identity in identifiers:
                    raise CliError('REMOTE_ERROR', 'Jira pagination changed or overlapped; rerun the query before treating it as complete.')
                identifiers.add(identity)
            items.extend(page)
            start += len(page)
            total = response.get('total')
            if response.get('isLast') is True or (isinstance(total, int) and start >= total):
                return items
        raise CliError('REMOTE_ERROR', 'Jira pagination exceeded the safety limit.')

    def search(self, jql: str, fields: List[str]) -> List[dict]:
        return self._pages(API + '/search', 'issues', {'jql': jql, 'fields': ','.join(fields)})

    def get_issue(self, key: str) -> dict:
        return checked_object(self.client.request('GET', issue_path(key)))

    def comments(self, key: str) -> List[dict]:
        return self._pages(issue_path(key) + '/comment', 'comments')

    def transitions(self, key: str) -> List[dict]:
        response = checked_object(self.client.request('GET', issue_path(key) + '/transitions', {'expand': 'transitions.fields'}))
        items = response.get('transitions')
        if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
            raise CliError('REMOTE_ERROR', 'Expected a list of Jira transitions.')
        return items

    def changelog(self, key: str) -> dict:
        response = checked_object(self.client.request('GET', issue_path(key), {'expand': 'changelog'}))
        history = checked_object(response.get('changelog', {}))
        entries = history.get('histories')
        complete = (isinstance(entries, list) and history.get('startAt', 0) == 0
                    and isinstance(history.get('total'), int) and len(entries) >= history['total'])
        return dict(history, complete=complete)

    def download_attachment(self, attachment: dict, output_dir: str) -> dict:
        aid = str(attachment.get('id', ''))
        if not re.fullmatch(r'[0-9]+', aid) or not isinstance(attachment.get('content'), str):
            raise CliError('REMOTE_ERROR', 'Attachment has no valid ID or download URL.')
        root = Path(output_dir).resolve()
        directory = archive_path(root, 'attachments', aid)
        directory.mkdir(parents=True, exist_ok=True)
        record = archive_path(root, 'attachments', aid, 'record.json')
        if record.exists():
            try:
                prior = json.loads(record.read_text(encoding='utf-8'))
                cached = archive_path(root, 'attachments', aid, prior['sha256'] + '.bin')
                if (prior['source_url'] == attachment['content'] and prior['size'] == attachment.get('size')
                        and cached.is_file() and file_digest(str(cached)) == prior['sha256']):
                    return dict(prior, path=str(cached), original_name=attachment.get('filename', ''))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        fd, staging = tempfile.mkstemp(prefix='.incoming-', dir=str(directory))
        os.close(fd)
        try:
            item = self.client.download(attachment['content'], staging)
            if isinstance(attachment.get('size'), int) and attachment['size'] != item['size']:
                raise CliError('REMOTE_ERROR', 'Attachment size differs from Jira metadata.')
            destination = archive_path(root, 'attachments', aid, item['sha256'] + '.bin')
            os.replace(staging, str(destination))
            item.update(id=aid, original_name=attachment.get('filename', ''), path=str(destination))
            write_json_atomic(str(record), item)
            return item
        finally:
            if os.path.exists(staging):
                os.unlink(staging)

    def fetch(self, key: str, output_dir: str) -> dict:
        issue_path(key)
        root = Path(output_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        for name in ('issue', 'attachments', 'issue.md'):
            archive_path(root, name)
        with file_lock(str(root / '.fetch.lock')):
            issue = self.get_issue(key)
            comments = self.comments(key)
            history = self.changelog(key)
            raw = {'issue': issue, 'comments': comments, 'changelog': history}
            snapshot_hash = digest(raw)
            snapshot = archive_path(root, 'issue', 'snapshots', snapshot_hash + '.json')
            write_json_atomic(str(snapshot), raw)
            artifacts, failures = [], []
            fields = checked_object(issue.get('fields', {}))
            attachments = fields.get('attachment', [])
            if not isinstance(attachments, list):
                raise CliError('REMOTE_ERROR', 'Expected an attachment list.')
            for attachment in attachments:
                try:
                    if not isinstance(attachment, dict):
                        raise CliError('REMOTE_ERROR', 'Invalid attachment metadata.')
                    artifacts.append(self.download_attachment(attachment, str(root)))
                except CliError as exc:
                    failures.append({'id': attachment.get('id') if isinstance(attachment, dict) else None,
                                     'error': exc.as_dict()})
                except OSError:
                    failures.append({'id': attachment.get('id') if isinstance(attachment, dict) else None,
                                     'error': CliError('LOCAL_ERROR', 'Cannot store this attachment; check its local path and permissions.').as_dict()})
            warnings = [] if history['complete'] else ['Changelog is incomplete or unavailable; inspect pagination metadata.']
            data = {'issue_key': key, 'snapshot_hash': snapshot_hash, 'snapshot_path': str(snapshot),
                    'artifacts': artifacts, 'failures': failures, 'warnings': warnings,
                    'fetched_at': datetime.now(timezone.utc).isoformat()}
            manifest = archive_path(root, 'issue', 'manifest.json')
            data['manifest_path'] = str(manifest)
            write_json_atomic(str(manifest), data)
            text = '# ' + str(issue.get('key', key)) + ': ' + str(fields.get('summary', '')) + '\n\n'
            text += 'Snapshot: `' + snapshot_hash + '`\n\n' + str(fields.get('description') or '') + '\n\n'
            for comment in comments:
                text += '---\n\n' + str(comment.get('created', '')) + '\n\n' + str(comment.get('body', '')) + '\n\n'
            if warnings or failures:
                text += '\nArchive warnings: ' + json.dumps(warnings + failures, ensure_ascii=False) + '\n'
            write_bytes_atomic(str(archive_path(root, 'issue.md')), text.encode('utf-8'))
            return data

    def _write_input(self, action, key, fields):
        issue_path(key)
        if not isinstance(fields, dict):
            raise CliError('INPUT', 'Write parameters must be an object.')
        if action == 'comment':
            if set(fields) != {'body'} or not isinstance(fields['body'], str) or not fields['body'].strip():
                raise CliError('INPUT', 'Comment requires non-empty text.')
            if '[jira-cli-operation:' in fields['body']:
                raise CliError('INPUT', 'Comment contains a reserved operation marker.')
        elif action == 'transition':
            if set(fields) - {'transition_id', 'fields'} or not str(fields.get('transition_id', '')).isdigit():
                raise CliError('INPUT', 'Transition requires a numeric transition_id.')
            if not isinstance(fields.get('fields', {}), dict):
                raise CliError('INPUT', 'Transition fields must be an object.')
        elif action == 'assign':
            if set(fields) != {'username'} or not isinstance(fields['username'], str) or not fields['username'].strip():
                raise CliError('INPUT', 'Assignment requires a Jira Server username.')
        else:
            raise CliError('INPUT', 'Unsupported write action.')

    def preview_write(self, action: str, key: str, fields: dict) -> dict:
        self._write_input(action, key, fields)
        path = issue_path(key)
        preview = {'action': action, 'issue_key': key}
        if action == 'comment':
            self.get_issue(key)
            return dict(preview, method='POST', path=path + '/comment', body={'body': fields['body']})
        if action == 'assign':
            current = checked_object(self.get_issue(key).get('fields', {}))
            users = self.client.request('GET', API + '/user/assignable/search',
                                        {'issueKey': key, 'username': fields['username'], 'maxResults': 1000})
            if not isinstance(users, list) or not any(isinstance(u, dict) and u.get('name') == fields['username'] for u in users):
                raise CliError('PRECONDITION', 'Username was not confirmed assignable to this issue.')
            return dict(preview, method='PUT', path=path + '/assignee', body={'name': fields['username']},
                        current_assignee=checked_object(current.get('assignee') or {}).get('name'))
        current = checked_object(self.get_issue(key).get('fields', {}))
        transition = next((t for t in self.transitions(key) if str(t.get('id')) == str(fields['transition_id'])), None)
        if transition is None:
            raise CliError('PRECONDITION', 'Transition is not currently available.')
        supplied = fields.get('fields', {})
        screen = checked_object(transition.get('fields', {}))
        for name, metadata in screen.items():
            if checked_object(metadata).get('required') and not metadata.get('hasDefaultValue') and supplied.get(name) in (None, '', [], {}):
                raise CliError('PRECONDITION', 'Required transition fields are missing.', {'field': name})
        unknown = set(supplied) - set(screen)
        if unknown:
            raise CliError('PRECONDITION', 'Fields are not available on this transition screen.', {'fields': sorted(unknown)})
        target = checked_object(transition.get('to', {})).get('id')
        if not isinstance(target, str):
            raise CliError('REMOTE_ERROR', 'Transition has no target status ID.')
        return dict(preview, method='POST', path=path + '/transitions', target_status=target,
                    current_status=checked_object(current.get('status') or {}).get('id'),
                    body={'transition': {'id': str(fields['transition_id'])}, 'fields': supplied})

    def execute_write(self, action: str, key: str, fields: dict, operation_id: str, state_dir: str, expected_preview_digest=None) -> dict:
        self._write_input(action, key, fields)
        actor = self.me().get('name')
        if not isinstance(actor, str) or not actor:
            raise CliError('AUTH_REQUIRED', 'Jira did not identify the authenticated Server username.')
        scope = {'service': self.client.base_url, 'actor': actor, 'action': action, 'key': key, 'fields': fields}
        if expected_preview_digest is not None:
            scope['expected_preview_digest'] = expected_preview_digest
        marker = '[jira-cli-operation:' + digest({'id': operation_id, 'service': self.client.base_url, 'key': key}) + ']'
        comment_body = (fields.get('body', '') + '\n\n' + marker) if action == 'comment' else ''

        def reconcile():
            if action == 'comment':
                matches = [c for c in self.comments(key) if marker in str(c.get('body', ''))]
                if len(matches) == 1:
                    item = matches[0]
                    if item.get('body') == comment_body and checked_object(item.get('author', {})).get('name') == actor:
                        return result('SUCCESS', {'action': action, 'comment': item})
                    return result('UNKNOWN', error={'code': 'RESULT_UNKNOWN', 'message': 'Operation marker exists with different content or author.'})
            return result('UNKNOWN', error={'code': 'RESULT_UNKNOWN', 'message': 'No conclusive receipt. Current status or assignee alone cannot prove this operation succeeded.'})

        def execute():
            preview = self.preview_write(action, key, fields)
            if expected_preview_digest is not None and digest(dict(preview, mode='preview', correlation_marker_added=action == 'comment')) != expected_preview_digest:
                raise CliError('CONFLICT', 'Jira preview changed after approval; prepare and approve it again.')
            body = preview['body']
            if action == 'comment':
                # Prevent duplicate comments even if the user moved their local receipt directory.
                existing = [c for c in self.comments(key) if marker in str(c.get('body', ''))]
                if existing:
                    return reconcile()
                body = {'body': comment_body}
            response = self.client.request(preview['method'], preview['path'], body=body)
            try:
                if action == 'comment':
                    cid = str(checked_object(response).get('id', ''))
                    if not cid.isdigit():
                        return reconcile()
                    item = checked_object(self.client.request('GET', issue_path(key) + '/comment/' + quote(cid, safe='')))
                    if item.get('body') != comment_body or checked_object(item.get('author', {})).get('name') != actor:
                        raise CliError('RESULT_UNKNOWN', 'Comment acknowledgement did not match readback.')
                    return result('SUCCESS', {'action': action, 'comment': item})
                current = checked_object(self.get_issue(key).get('fields', {}))
                if action == 'transition' and checked_object(current.get('status', {})).get('id') == preview['target_status']:
                    return result('SUCCESS', {'action': action, 'target_status': preview['target_status'], 'acknowledged': True})
                if action == 'assign' and checked_object(current.get('assignee', {})).get('name') == fields['username']:
                    return result('SUCCESS', {'action': action, 'assignee': fields['username'], 'acknowledged': True})
                raise CliError('RESULT_UNKNOWN', 'Write was acknowledged but the current issue differs; inspect concurrent changes.')
            except CliError as exc:
                raise CliError('RESULT_UNKNOWN', 'Write readback could not confirm its effect.', {'cause': exc.code})

        return run_action(state_dir, operation_id, scope, execute, reconcile)
