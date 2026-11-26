import multiprocessing
import tempfile
import unittest
from pathlib import Path

from myjira_cli.actions import run_action
from myjira_cli.common import CliError, result


def hold_action(directory, ready, release):
    def execute():
        ready.set()
        release.wait(5)
        return result('SUCCESS', {'done': True})
    run_action(directory, 'op', {'x': 1}, execute, lambda: result('UNKNOWN'))


class ActionTests(unittest.TestCase):
    def test_same_operation_different_payload_conflicts(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            execute = lambda: calls.append(1) or result('SUCCESS', {'id': 1})
            a = run_action(d, 'op', {'x': 1}, execute, lambda: result('UNKNOWN'))
            b = run_action(d, 'op', {'x': 1}, execute, lambda: result('UNKNOWN'))
            self.assertEqual(a['status'], b['status'])
            self.assertEqual(len(calls), 1)
            with self.assertRaises(CliError):
                run_action(d, 'op', {'x': 2}, execute, lambda: result('UNKNOWN'))

    def test_unknown_is_reconciled_not_reexecuted(self):
        with tempfile.TemporaryDirectory() as d:
            def lost():
                raise CliError('RESULT_UNKNOWN', 'lost')
            a = run_action(d, 'op', {'x': 1}, lost, lambda: result('UNKNOWN'))
            self.assertEqual(a['status'], 'UNKNOWN')
            b = run_action(d, 'op', {'x': 1}, lambda: self.fail('must not execute twice'), lambda: result('SUCCESS', {'id': 1}))
            self.assertEqual(b['status'], 'SUCCESS')

    def test_concurrent_operation_has_one_executor(self):
        with tempfile.TemporaryDirectory() as d:
            ready, release = multiprocessing.Event(), multiprocessing.Event()
            p = multiprocessing.Process(target=hold_action, args=(d, ready, release))
            p.start()
            try:
                self.assertTrue(ready.wait(5))
                with self.assertRaises(CliError):
                    run_action(d, 'op', {'x': 1}, lambda: self.fail('concurrent execution'), lambda: result('UNKNOWN'))
            finally:
                release.set()
                p.join(5)
            self.assertEqual(p.exitcode, 0)

    def test_keyboard_interrupt_preserves_uncertainty(self):
        with tempfile.TemporaryDirectory() as d:
            def interrupt():
                raise KeyboardInterrupt()
            self.assertEqual(run_action(d, 'op', {}, interrupt, lambda: result('UNKNOWN'))['status'], 'UNKNOWN')
