import unittest
from myjira_cli.common import CliError
from myjira_cli.jira import Jira
from tests.helpers import ScriptedClient


class JiraReadTests(unittest.TestCase):
    def test_overlapping_pages_are_reported_incomplete(self):
        def page(m, p, q, b):
            if q['startAt'] == 0:
                return {'startAt': 0, 'total': 3, 'issues': [{'id': '1'}, {'id': '2'}]}
            return {'startAt': 2, 'total': 4, 'issues': [{'id': '2', 'changed': True}, {'id': '3'}]}
        with self.assertRaises(CliError) as caught:
            Jira(ScriptedClient(page)).search('x', [])
        self.assertEqual(caught.exception.code, 'REMOTE_ERROR')

    def test_search_uses_returned_page_size(self):
        def page(method, path, q, body):
            self.assertEqual(method, 'GET')
            self.assertEqual(q['jql'], 'assignee = currentUser()')
            start = q['startAt']
            return {'startAt': start, 'maxResults': 2, 'total': 3,
                    'issues': [{'id': str(i)} for i in range(start, min(start + 2, 3))]}
        c = ScriptedClient(page)
        items = Jira(c).search('assignee = currentUser()', ['summary'])
        self.assertEqual([x['id'] for x in items], ['0', '1', '2'])
        self.assertEqual([x[2]['startAt'] for x in c.calls], [0, 2])

    def test_empty_or_repeated_page_terminates(self):
        self.assertEqual(Jira(ScriptedClient(lambda *a: {'issues': [], 'total': 99})).search('x', []), [])
        with self.assertRaises(CliError):
            Jira(ScriptedClient(lambda *a: {'issues': [{'id': '1'}], 'total': 99})).search('x', [])

    def test_comments_are_fully_paged(self):
        def page(m, p, q, b):
            return {'startAt': q['startAt'], 'total': 2, 'comments': [{'id': str(q['startAt'])}]}
        c = ScriptedClient(page)
        self.assertEqual(len(Jira(c).comments('BUG-1')), 2)
        self.assertTrue(all(call[0] == 'GET' for call in c.calls))

    def test_transition_reads_required_fields(self):
        c = ScriptedClient(lambda *a: {'transitions': [{'id': '21', 'fields': {'resolution': {'required': True}}}]})
        t = Jira(c).transitions('BUG-1')
        self.assertEqual(t[0]['id'], '21')
        self.assertTrue(t[0]['fields']['resolution']['required'])
        self.assertEqual(c.calls[0][2]['expand'], 'transitions.fields')

    def test_changelog_truncation_is_explicit(self):
        c = ScriptedClient(lambda *a: {'changelog': {'startAt': 0, 'maxResults': 1, 'total': 3, 'histories': [{'id': 'h'}]}})
        history = Jira(c).changelog('BUG-1')
        self.assertFalse(history['complete'])
        self.assertEqual(history['total'], 3)

    def test_unexpected_shape_is_not_success(self):
        with self.assertRaises(CliError):
            Jira(ScriptedClient(lambda *a: ['unexpected'])).get_issue('BUG-1')

    def test_issue_key_cannot_inject_path(self):
        c = ScriptedClient(lambda *a: {})
        with self.assertRaises(CliError):
            Jira(c).get_issue('../myself?x')
        self.assertEqual(c.calls, [])
