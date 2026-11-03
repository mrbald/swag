"""Contract checks for suite selection, execution and truthful gate results."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    'check_vncws', Path(__file__).resolve().parents[1] / 'tools/check_vncws.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class FunctionalGateTests(unittest.TestCase):
    def invoke(self, directory, *args, execute=None):
        stdout = io.StringIO()
        with patch.object(gate.subprocess, 'run', side_effect=execute) as called, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = gate.main(['--output', str(directory), *args])
        return code, json.loads(stdout.getvalue()), called

    @staticmethod
    def passed(argv, **kwargs):
        output = Path(argv[argv.index('--output') + 1])
        (output / 'results.json').write_text(json.dumps([
            {'backend': 'qt-ws', 'checks': [{'check': 'actual interaction', 'status': 'pass'}]}]))
        return subprocess.CompletedProcess(argv, 0)

    def test_list_has_no_execution_or_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'unused'
            code, report, called = self.invoke(output, '--list')
            self.assertEqual(code, 0)
            called.assert_not_called()
            self.assertFalse(output.exists())
            defaults = [row['name'] for row in report['suites'] if row['default']]
            self.assertEqual(defaults, ['protocol', 'input', 'clipboard', 'compression',
                                        'resize', 'smoke', 'everyday', 'outbound-clipboard'])
            self.assertFalse(any(row['shared_demo'] for row in report['suites'] if row['default']))

    def test_selection_is_ordered_deduplicated_and_passes_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, report, called = self.invoke(temporary, '--suite', 'app-mdi', '--suite', 'input',
                '--suite', 'input', '--engine', 'docker', '--port', '6400',
                '--origin', 'http://localhost:7000', execute=self.passed)
            self.assertEqual(code, 0)
            self.assertEqual(report['selected_suites'], ['input', 'app-mdi'])
            self.assertEqual(called.call_count, 2)
            first, second = [call.args[0] for call in called.call_args_list]
            self.assertIn('6400', first)
            self.assertIn('docker', first)
            self.assertIn('http://localhost:7000', second)
            self.assertIn('qt-ws', second)
            self.assertEqual(report['checks'], 2)
            stored = json.loads((Path(report['output']) / 'summary.json').read_text())
            self.assertEqual(stored, report)

    def test_nonzero_exit_fails_even_when_report_passes_and_stops_later_suites(self):
        def failed(argv, **kwargs):
            self.passed(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 7)
        with tempfile.TemporaryDirectory() as temporary:
            code, report, called = self.invoke(temporary, '--suite', 'input', '--suite', 'smoke',
                                               execute=failed)
            self.assertEqual(code, 1)
            self.assertEqual(called.call_count, 1)
            self.assertEqual([row['status'] for row in report['suites']], ['fail', 'skipped'])
            self.assertEqual(report['suites'][0]['returncode'], 7)

    def test_outbound_clipboard_receives_selected_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, _, called = self.invoke(temporary, '--suite', 'outbound-clipboard',
                                           '--engine', 'docker', '--port', '6402', '--backend', 'qt-unix', execute=self.passed)
            self.assertEqual(code, 0)
            argv = called.call_args.args[0]
            self.assertTrue(argv[1].endswith('tests/browser/outbound_clipboard.py'))
            self.assertIn('docker', argv)
            self.assertIn('6402', argv)
            self.assertEqual(argv[argv.index('--backend')+1], 'qt-unix')
            self.assertNotIn('--workspace-origin', argv)

    def test_unix_backend_reaches_wire_browser_and_application_suites(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, report, called = self.invoke(temporary, '--backend', 'qt-unix',
                '--suite', 'input', '--suite', 'smoke', '--suite', 'app-mdi', execute=self.passed)
            self.assertEqual(code, 0)
            self.assertEqual(report['backend'], 'qt-unix')
            for call in called.call_args_list:
                self.assertIn('qt-unix', call.args[0])

    def test_failed_assertion_overrides_zero_exit_and_keep_going_runs_later_suite(self):
        def execute(argv, **kwargs):
            result = self.passed(argv, **kwargs)
            if argv[1].endswith('vncws_input.py'):
                output = Path(argv[argv.index('--output') + 1])
                (output / 'results.json').write_text(
                    '{"checks": [{"check": "lost input", "status": "fail"}]}')
            return result
        with tempfile.TemporaryDirectory() as temporary:
            code, report, called = self.invoke(temporary, '--suite', 'input', '--suite', 'smoke',
                                               '--keep-going', execute=execute)
            self.assertEqual(code, 1)
            self.assertEqual(called.call_count, 2)
            self.assertEqual([row['status'] for row in report['suites']], ['fail', 'pass'])
            self.assertEqual(report['suites'][0]['failed_checks'], ['lost input'])

    def test_missing_or_empty_report_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            for body in (None, '[]', 'not json'):
                with self.subTest(body=body):
                    def execute(argv, **kwargs):
                        if body is not None:
                            output = Path(argv[argv.index('--output') + 1])
                            (output / 'results.json').write_text(body)
                        return subprocess.CompletedProcess(argv, 0)
                    code, report, _ = self.invoke(temporary, '--suite', 'input', execute=execute)
                    self.assertEqual(code, 1)
                    self.assertEqual(report['status'], 'fail')

    def test_runs_do_not_overwrite_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, first, _ = self.invoke(temporary, '--suite', 'input', execute=self.passed)
            _, second, _ = self.invoke(temporary, '--suite', 'input', execute=self.passed)
            self.assertNotEqual(first['output'], second['output'])
            self.assertTrue((Path(first['output']) / 'input/results.json').is_file())
            self.assertTrue((Path(second['output']) / 'input/results.json').is_file())


if __name__ == '__main__':
    unittest.main()
