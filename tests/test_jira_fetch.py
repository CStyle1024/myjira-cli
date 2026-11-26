import json
import os
import tempfile
import unittest
from pathlib import Path

from myjira_cli.common import CliError
from myjira_cli.http import Client
from myjira_cli.jira import Jira
from tests.helpers import ScriptedClient, Response, Opener


class DownloadClient(ScriptedClient):
    def __init__(self, attachments=None, fail=False):
        self.attachments = attachments or []
        self.fail = fail
        self.downloads = 0
        self.summary = 'First summary'
        super().__init__(self.handle)

    def handle(self, m, path, q, b):
        if path.endswith('/comment'):
            return {'comments': [], 'total': 0}
        if q.get('expand'):
            return {'changelog': {'histories': [], 'startAt': 0, 'total': 0}}
        return {'key': 'BUG-1', 'fields': {'summary': self.summary, 'attachment': self.attachments}}

    def download(self, url, path):
        self.downloads += 1
        client = Client('https://jira.test', {'type': 'none'}, 1)
        client.opener = Opener(Response(b'abc', content_type='application/octet-stream', headers={'Content-Length': '30' if self.fail else '3'}))
        return client.download(url, path)


def attachment(aid='1', filename='log.txt'):
    return {'id': aid, 'filename': filename, 'size': 3, 'content': 'https://jira.test/secure/attachment/' + aid}


class FetchTests(unittest.TestCase):
    def test_local_attachment_failure_records_manifest_and_continues(self):
        c = DownloadClient([attachment('1'), attachment('2')])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'attachments').mkdir()
            (Path(d) / 'attachments' / '1').write_bytes(b'not a directory')
            data = Jira(c).fetch('BUG-1', d)
            self.assertEqual([x['id'] for x in data['artifacts']], ['2'])
            self.assertEqual(data['failures'][0]['error']['code'], 'LOCAL_ERROR')
            manifest = json.loads(Path(data['manifest_path']).read_text())
            self.assertEqual(manifest['failures'][0]['id'], '1')
            self.assertEqual((Path(d) / 'attachments' / '1').read_bytes(), b'not a directory')

    def test_attachment_redirect_does_not_forward_secret(self):
        client = Client('https://jira.test', {'type': 'none'}, 1)
        client.authorization = 'Bearer attachment-secret'
        client.opener = Opener(Response(b'abc', content_type='application/octet-stream', url='https://files.test/log'))
        with tempfile.TemporaryDirectory() as d:
            client.download('https://files.test/log', d + '/log')
        self.assertIsNone(client.opener.requests[0].get_header('Authorization'))

    def test_fetch_keeps_previous_snapshot(self):
        c = DownloadClient()
        with tempfile.TemporaryDirectory() as d:
            first = Jira(c).fetch('BUG-1', d)
            c.summary = 'New summary'
            second = Jira(c).fetch('BUG-1', d)
            self.assertNotEqual(first['snapshot_hash'], second['snapshot_hash'])
            self.assertEqual(len(list((Path(d) / 'issue' / 'snapshots').glob('*.json'))), 2)
            self.assertTrue((Path(d) / 'issue.md').exists())

    def test_same_name_attachments_use_id(self):
        c = DownloadClient([attachment('1'), attachment('2')])
        with tempfile.TemporaryDirectory() as d:
            data = Jira(c).fetch('BUG-1', d)
            self.assertEqual(data['failures'], [])
            self.assertNotEqual(data['artifacts'][0]['path'], data['artifacts'][1]['path'])
            Jira(c).fetch('BUG-1', d)
            self.assertEqual(c.downloads, 2)

    def test_traversal_name_stays_inside_root(self):
        c = DownloadClient([attachment('1', '../../escape')])
        with tempfile.TemporaryDirectory() as d:
            data = Jira(c).fetch('BUG-1', d)
            self.assertEqual(data['artifacts'][0]['original_name'], '../../escape')
            self.assertTrue(Path(data['artifacts'][0]['path']).resolve().is_relative_to(Path(d)) if hasattr(Path, 'is_relative_to') else str(Path(data['artifacts'][0]['path']).resolve()).startswith(d + '/'))

    def test_partial_download_never_becomes_complete(self):
        c = DownloadClient([attachment()], fail=True)
        with tempfile.TemporaryDirectory() as d:
            data = Jira(c).fetch('BUG-1', d)
            self.assertEqual(len(data['failures']), 1)
            self.assertEqual(data['artifacts'], [])
            self.assertFalse(list(Path(d).rglob('*.bin')))
            c.fail = False
            self.assertEqual(Jira(c).fetch('BUG-1', d)['failures'], [])

    def test_attachment_metadata_size_is_checked(self):
        a = attachment()
        a['size'] = 9
        with tempfile.TemporaryDirectory() as d:
            data = Jira(DownloadClient([a])).fetch('BUG-1', d)
            self.assertEqual(len(data['failures']), 1)

    def test_symlink_directory_cannot_escape_archive(self):
        c = DownloadClient([attachment()])
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            os.symlink(outside, Path(d) / 'attachments')
            with self.assertRaises(CliError):
                Jira(c).fetch('BUG-1', d)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_cached_attachment_is_rechecked(self):
        c = DownloadClient([attachment()])
        with tempfile.TemporaryDirectory() as d:
            first = Jira(c).fetch('BUG-1', d)
            Path(first['artifacts'][0]['path']).write_bytes(b'broken')
            second = Jira(c).fetch('BUG-1', d)
            self.assertEqual(Path(second['artifacts'][0]['path']).read_bytes(), b'abc')
            self.assertEqual(c.downloads, 2)
