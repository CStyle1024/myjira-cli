import json, unittest
from tests.helpers import invoke
from myjira_cli.cli import main

class IdentityTests(unittest.TestCase):
    def test_identity_needs_no_config_or_network(self):
        code, out, err = invoke(main, ['--tool-info'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['distribution'], 'myjira-cli')
        self.assertEqual(json.loads(out)['result_schema'], 1)
