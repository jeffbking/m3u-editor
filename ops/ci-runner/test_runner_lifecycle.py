"""Exercise cleanup after transport failure without Docker, SSH or GitHub access."""
import json
import pathlib
import runpy
import shutil
import signal
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class RunnerLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))

    def test_archive_and_api_failures_do_not_skip_teardown_or_mask_job_error(self):
        self.exercise_cleanup()

    def test_failed_removal_preserves_instance_for_recovery(self):
        self.exercise_cleanup(removal_fails=True)

    def exercise_cleanup(self, removal_fails=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            source = pathlib.Path(__file__).with_name('run-ephemeral-vm.py')
            script = root / source.name
            shutil.copyfile(source, script)
            (root / 'operator_key.pub').write_text('ssh-ed25519 test-only-key\n')
            stale = root / 'instances' / '5900xt-tripslop-pr-ci-abandoned'
            stale.mkdir(parents=True)
            (stale / 'disk.qcow2').write_text('old overlay')
            calls = []
            job_failed = False
            original_open = pathlib.Path.open
            original_stat = pathlib.Path.stat

            def command(args, **kwargs):
                nonlocal job_failed
                calls.append(args)
                if removal_fails and args[:2] == ['docker', 'rm']:
                    raise subprocess.TimeoutExpired(args, 40)
                if args[0] == 'ssh' and 'input' in kwargs:
                    job_failed = True
                    raise subprocess.CalledProcessError(42, args)
                return subprocess.CompletedProcess(args, 0)

            def output(args, **kwargs):
                if args[:3] == ['docker', 'network', 'inspect']:
                    return json.dumps([{'Driver':'bridge','EnableIPv6':False,'IPAM':{'Config':[{'Subnet':'10.89.0.0/24'}]}}])
                if args[:3] == ['docker', 'ps', '-aq']:
                    return ''
                if args[-1].endswith('registration-token'):
                    return json.dumps({'token': 'test-registration-token'})
                if job_failed:
                    raise subprocess.CalledProcessError(503, args)
                return json.dumps([{'runners': []}])

            def stat_file(path, *args, **kwargs):
                if str(path) == '/dev/kvm':
                    return SimpleNamespace(st_gid=0)
                return original_stat(path, *args, **kwargs)

            def open_file(path, *args, **kwargs):
                if path.name == 'runner-diag.tar':
                    raise OSError('simulated archive disk failure')
                return original_open(path, *args, **kwargs)

            with patch('sys.argv', [str(script), 'Tripslop', '22204', '3072']), \
                 patch('shutil.disk_usage', return_value=SimpleNamespace(free=100 * 1024**3)), \
                 patch('subprocess.run', side_effect=command), \
                 patch('subprocess.check_output', side_effect=output), \
                 patch.object(pathlib.Path, 'open', open_file), \
                 patch.object(pathlib.Path, 'stat', stat_file):
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    runpy.run_path(str(script))
            self.assertEqual(failure.exception.returncode, 42)
            self.assertIn(['docker', 'logs', '--tail', '1000', '5900xt-tripslop-ci-vm'], calls)
            self.assertIn(['docker', 'stop', '--timeout', '30', '5900xt-tripslop-ci-vm'], calls)
            self.assertIn(['docker', 'rm', '-f', '5900xt-tripslop-ci-vm'], calls)
            remaining = list((root / 'instances').iterdir())
            self.assertEqual(len(remaining), 1 if removal_fails else 0)
            self.assertFalse(stale.exists())
            registration = next(c[-1] for c in calls if c[0] == 'ssh' and '--labels' in c[-1])
            self.assertIn('--labels 5900xt-tripslop-pr-ci ', registration)


if __name__ == '__main__':
    unittest.main()
