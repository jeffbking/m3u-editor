#!/usr/bin/env python3
"""Build a credential-free, flattened QEMU image from verified upstream inputs."""
import hashlib
import json
import pathlib
import subprocess
import sys
import uuid
import shutil
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_fixed

root = pathlib.Path(__file__).resolve().parent
image = 'local/ci-qemu:2026-09-13'
base = root / 'noble-server-cloudimg-amd64.img'
archive = root / 'actions-runner-linux-x64-2.337.0.tar.gz'
for path, digest in [(base, '612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354'),
                     (archive, '70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613')]:
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise SystemExit(f'Checksum mismatch: {path.name}')
if (root / 'golden.qcow2').exists():
    raise SystemExit('golden.qcow2 exists; use a new build directory for image maintenance')
directory = root / ('template-build-' + uuid.uuid4().hex[:12])
directory.mkdir()
container = directory.name
config = json.loads((root / 'cloud-config.json').read_text())
config['hostname'] = container
config['users'][0]['ssh_authorized_keys'] = [(root / 'operator_key.pub').read_text().strip()]
(directory / 'user-data').write_text('#cloud-config\n' + json.dumps(config) + '\n')
(directory / 'meta-data').write_text(f'instance-id: {container}\nlocal-hostname: {container}\n')
mounts = ['-v', f'{directory}:/vm', '-v', f'{base}:/base.img:ro']
def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)

ssh = ['ssh', '-i', str(root / 'operator_key'), '-p', '22209', '-o', 'BatchMode=yes',
       '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new',
       '-o', f'UserKnownHostsFile={directory}/known_hosts', 'runner@127.0.0.1']
@retry(retry=retry_if_exception_type((subprocess.CalledProcessError, subprocess.TimeoutExpired)),
       stop=stop_after_delay(180), wait=wait_fixed(3), reraise=True)
def await_ssh():
    run(ssh + ['true'], timeout=10)

try:
    run(['docker', 'run', '--pull=never', '--rm', '--entrypoint', 'cloud-localds', *mounts, image,
         '/vm/seed.img', '/vm/user-data', '/vm/meta-data'])
    run(['docker', 'run', '--pull=never', '--rm', '--entrypoint', 'qemu-img', *mounts, image,
         'create', '-f', 'qcow2', '-F', 'qcow2', '-b', '/base.img', '/vm/disk.qcow2', '60G'])
    run(['docker', 'run', '--pull=never', '-d', '--name', container, '--network', 'ci-vms',
         '--device', '/dev/kvm', '--group-add', str(pathlib.Path('/dev/kvm').stat().st_gid),
         '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--cpus', '2',
         '--memory', '2816m', '--pids-limit', '128', '-p', '127.0.0.1:22209:2222',
         *mounts, image, '-enable-kvm', '-cpu', 'host', '-smp', '2', '-m', '2048',
         '-display', 'none', '-serial', 'file:/vm/serial.log',
         '-drive', 'file=/vm/disk.qcow2,if=virtio,format=qcow2',
         '-drive', 'file=/vm/seed.img,if=virtio,format=raw,readonly=on',
         '-netdev', 'user,id=net0,ipv6=off,hostfwd=tcp::2222-:22', '-device', 'virtio-net-pci,netdev=net0'])
    await_ssh()
    run(ssh + ['cloud-init status --wait'], timeout=900)
    # Reconnect after provisioning grants the Docker group.
    run(ssh + ['docker info && test ! -e /home/runner/actions-runner/.runner'])
    with archive.open('rb') as stream:
        run(ssh + ['tar xzf - -C /home/runner/actions-runner'], stdin=stream)
    run(ssh + ['sudo cloud-init clean --logs --machine-id && sudo rm -f /etc/ssh/ssh_host_* && sudo sync && sudo poweroff'])
    status = subprocess.check_output(['docker', 'wait', container], text=True, timeout=90).strip()
    if status != '0':
        raise SystemExit(f'Template did not shut down cleanly: {status}')
    run(['docker', 'run', '--pull=never', '--rm', '--entrypoint', 'qemu-img', *mounts,
         '-v', f'{root}:/output', image, 'convert', '-f', 'qcow2', '-O', 'qcow2',
         '/vm/disk.qcow2', '/output/golden.qcow2'])
    (root / 'golden.qcow2').chmod(0o444)
finally:
    try:
        subprocess.run(['docker', 'rm', '-f', container], timeout=40)
    except (OSError, subprocess.SubprocessError) as error:
        print(f'Template container cleanup failed: {error}', file=sys.stderr)
    try:
        logs = root / 'logs' / container
        logs.mkdir(parents=True, exist_ok=True)
        if (directory / 'serial.log').exists():
            shutil.copyfile(directory / 'serial.log', logs / 'serial.log')
    except OSError as error:
        print(f'Template log archive failed: {error}', file=sys.stderr)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
