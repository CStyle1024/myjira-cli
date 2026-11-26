import json
import tempfile
import unittest
from pathlib import Path

from myjira_cli.common import CliError, digest
from myjira_cli.jira import Jira
from myjira_cli.http import Client
from tests.helpers import ScriptedClient, Opener, Response


class WriteClient(ScriptedClient):
    def __init__(self):
        self.comment_list = []
        self.status = '1'
        self.assignee = 'me'
        self.drop = False
        self.required = False
        super().__init__(self.handle)

    def handle(self, m, p, q, b):
        if p.endswith('/myself'):
            return {'name': 'me'}
        if p.endswith('/user/assignable/search'):
            return [{'name': 'tester'}]
        if p.endswith('/transitions'):
            if m == 'GET':
                return {'transitions': [{'id': '21', 'to': {'id': '3'}, 'fields': {'resolution': {'required': self.required}}}]}
            self.status = '3'
            if self.drop:
                raise CliError('RESULT_UNKNOWN', 'connection lost')
            return None
        if p.endswith('/assignee'):
            self.assignee = b['name']
            if self.drop:
                raise CliError('RESULT_UNKNOWN', 'connection lost')
            return None
        if p.endswith('/comment'):
            if m == 'POST':
                item = {'id': str(len(self.comment_list) + 1), 'body': b['body'], 'author': {'name': 'me'}}
                self.comment_list.append(item)
                if self.drop:
                    raise CliError('RESULT_UNKNOWN', 'connection lost')
                return item
            return {'comments': self.comment_list, 'total': len(self.comment_list), 'startAt': 0}
        if '/comment/' in p:
            return self.comment_list[int(p.rsplit('/', 1)[1]) - 1]
        return {'key': 'BUG-1', 'fields': {'status': {'id': self.status}, 'assignee': {'name': self.assignee}}}


class WriteTests(unittest.TestCase):
    def test_approved_assignment_rejects_changed_assignee(self):
        c = WriteClient()
        jira = Jira(c)
        fields = {'username': 'tester'}
        preview = jira.preview_write('assign', 'BUG-1', fields)
        approved = digest(dict(preview, mode='preview', correlation_marker_added=False))
        c.assignee = 'new-owner'
        with tempfile.TemporaryDirectory() as d:
            out = jira.execute_write('assign', 'BUG-1', fields, 'op', d, approved)
        self.assertEqual(out['status'], 'FAILED')
        self.assertEqual(c.assignee, 'new-owner')
        self.assertFalse(any(x[0] == 'PUT' for x in c.calls))

    def test_html_after_comment_write_stays_unknown_then_reconciles(self):
        class HtmlWriteClient(WriteClient):
            def handle(self, m, p, q, b):
                value = super().handle(m, p, q, b)
                if m == 'POST' and p.endswith('/comment'):
                    transport = Client('https://jira.test', {'type': 'none'}, 1)
                    transport.opener = Opener(Response(b'<html>gateway</html>', content_type='text/html'))
                    return transport.request('POST', p, body=b)
                return value
        c = HtmlWriteClient()
        with tempfile.TemporaryDirectory() as d:
            first = Jira(c).execute_write('comment', 'BUG-1', {'body': 'evidence'}, 'op', d)
            self.assertEqual(first['status'], 'UNKNOWN')
            self.assertEqual(Jira(c).execute_write('comment', 'BUG-1', {'body': 'evidence'}, 'op', d)['status'], 'SUCCESS')
            self.assertEqual(len(c.comment_list), 1)

    def test_preview_makes_no_post(self):
        c = WriteClient()
        preview = Jira(c).preview_write('transition', 'BUG-1', {'transition_id': '21', 'fields': {}})
        self.assertEqual(preview['target_status'], '3')
        self.assertTrue(all(x[0] == 'GET' for x in c.calls))

    def test_missing_transition_fields_stops(self):
        c = WriteClient()
        c.required = True
        with tempfile.TemporaryDirectory() as d:
            out = Jira(c).execute_write('transition', 'BUG-1', {'transition_id': '21', 'fields': {}}, 'op', d)
            self.assertEqual(out['status'], 'FAILED')
            self.assertFalse(any(x[0] == 'POST' for x in c.calls))

    def test_lost_comment_reply_reconciles_without_duplicate(self):
        c = WriteClient()
        c.drop = True
        with tempfile.TemporaryDirectory() as d:
            first = Jira(c).execute_write('comment', 'BUG-1', {'body': 'fix evidence'}, 'op', d)
            self.assertEqual(first['status'], 'UNKNOWN')
            second = Jira(c).execute_write('comment', 'BUG-1', {'body': 'fix evidence'}, 'op', d)
            self.assertEqual(second['status'], 'SUCCESS')
            self.assertEqual(len(c.comment_list), 1)

    def test_ambiguous_transition_stays_unknown(self):
        c = WriteClient()
        c.drop = True
        with tempfile.TemporaryDirectory() as d:
            jira = Jira(c)
            self.assertEqual(jira.execute_write('transition', 'BUG-1', {'transition_id': '21'}, 'op', d)['status'], 'UNKNOWN')
            self.assertEqual(c.status, '3')
            self.assertEqual(jira.execute_write('transition', 'BUG-1', {'transition_id': '21'}, 'op', d)['status'], 'UNKNOWN')
            self.assertEqual(len([x for x in c.calls if x[0] == 'POST']), 1)

    def test_assign_verifies_username_and_effect(self):
        c = WriteClient()
        with tempfile.TemporaryDirectory() as d:
            out = Jira(c).execute_write('assign', 'BUG-1', {'username': 'tester'}, 'op', d)
            self.assertEqual(out['status'], 'SUCCESS')
            self.assertEqual(c.assignee, 'tester')

    def test_successful_transition_requires_ack_and_readback(self):
        c = WriteClient()
        with tempfile.TemporaryDirectory() as d:
            out = Jira(c).execute_write('transition', 'BUG-1', {'transition_id': '21'}, 'op', d)
            self.assertEqual(out['status'], 'SUCCESS')
            self.assertEqual(out['data']['target_status'], '3')

    def test_same_operation_different_comment_conflicts(self):
        c = WriteClient()
        with tempfile.TemporaryDirectory() as d:
            Jira(c).execute_write('comment', 'BUG-1', {'body': 'one'}, 'op', d)
            with self.assertRaises(CliError):
                Jira(c).execute_write('comment', 'BUG-1', {'body': 'two'}, 'op', d)
            self.assertEqual(len(c.comment_list), 1)

    def test_no_credentials_in_receipt(self):
        c = WriteClient()
        c.authorization = 'Bearer super-secret'
        with tempfile.TemporaryDirectory() as d:
            Jira(c).execute_write('comment', 'BUG-1', {'body': 'evidence'}, 'op', d)
            receipts = ''.join(p.read_text() for p in Path(d).rglob('*.json'))
            self.assertNotIn('super-secret', receipts)
            self.assertNotIn('authorization', receipts.lower())

    def test_unassignable_user_rejected(self):
        c = WriteClient()
        with self.assertRaises(CliError):
            Jira(c).preview_write('assign', 'BUG-1', {'username': 'other'})
        self.assertFalse(any(x[0] == 'PUT' for x in c.calls))
