"""Exercise cleanup after transport failure without Docker, SSH or GitHub access."""
import json
import pathlib
import runpy
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class RunnerLifecycleTest(unittest.TestCase):
    def test_archive_and_api_failures_do_not_skip_teardown_or_mask_job_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            source = pathlib.Path(__file__).with_name('run-ephemeral-vm.py')
            script = root / source.name
            shutil.copyfile(source, script)
            (root / 'operator_key.pub').write_text('ssh-ed25519 test-only-key\n')
            calls = []
            job_failed = False
            original_open = pathlib.Path.open
            original_stat = pathlib.Path.stat

            def command(args, **kwargs):
                nonlocal job_failed
                calls.append(args)
                if args[0] == 'ssh' and 'input' in kwargs:
                    job_failed = True
                    raise subprocess.CalledProcessError(42, args)
                return subprocess.CompletedProcess(args, 0)

            def output(args, **kwargs):
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
                 patch('subprocess.run', side_effect=command), \
                 patch('subprocess.check_output', side_effect=output), \
                 patch.object(pathlib.Path, 'open', open_file), \
                 patch.object(pathlib.Path, 'stat', stat_file):
                with self.assertRaises(subprocess.CalledProcessError) as failure:
                    runpy.run_path(str(script))
            self.assertEqual(failure.exception.returncode, 42)
            self.assertIn(['docker', 'stop', '--timeout', '30', '5900xt-tripslop-ci-vm'], calls)
            self.assertIn(['docker', 'rm', '-f', '5900xt-tripslop-ci-vm'], calls)
            self.assertEqual(list((root / 'instances').iterdir()), [])
            registration = next(c[-1] for c in calls if c[0] == 'ssh' and '--labels' in c[-1])
            self.assertIn('--labels 5900xt-tripslop-pr-ci ', registration)


if __name__ == '__main__':
    unittest.main()
