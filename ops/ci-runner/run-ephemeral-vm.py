#!/usr/bin/env python3
"""One disposable QEMU guest per Actions job; systemd owns restart/backoff."""
import json
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import uuid
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_fixed

root = pathlib.Path(__file__).resolve().parent
if len(sys.argv) != 4:
    raise SystemExit('Usage: run-ephemeral-vm.py REPOSITORY PORT MEMORY_MIB')
repo, port_text, memory_text = sys.argv[1:4]
if not re.fullmatch(r'[A-Za-z0-9_.-]+', repo) or repo in {'.', '..'}:
    raise SystemExit('Invalid repository')
port, memory = int(port_text), int(memory_text)
if not (1024 <= port <= 65535 and 1024 <= memory <= 8192):
    raise SystemExit('Port or memory outside supported range')
label = f'5900xt-{repo.lower()}-' + ('pr-ci' if repo in ['gitdock', 'tripslop'] else 'ci')
name = f'{label}-{uuid.uuid4().hex[:12]}'
container = f'5900xt-{repo.lower()}-ci-vm'
directory = root / 'instances' / name
directory.mkdir(parents=True)
image = 'local/ci-qemu:2026-09-13'
mounts = ['-v', f'{directory}:/vm', '-v', f'{root}/golden.qcow2:/golden.img:ro']

def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)

@retry(retry=retry_if_exception_type((subprocess.CalledProcessError, subprocess.TimeoutExpired)),
       stop=stop_after_delay(120), wait=wait_fixed(3), reraise=True)
def await_ssh():
    run(ssh + ['true'], timeout=10)

def stop(signum, _frame):
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, stop)
ssh = ['ssh', '-i', str(root / 'operator_key'), '-p', str(port),
       '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=24',
       '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
       '-o', 'StrictHostKeyChecking=accept-new',
       '-o', f'UserKnownHostsFile={directory}/known_hosts', 'runner@127.0.0.1']
try:
    run(['docker', 'run', '--rm', '--network', 'host', '--cap-drop', 'ALL',
         '--cap-add', 'NET_ADMIN', '--security-opt', 'no-new-privileges',
         'local/ci-runner-firewall:2026-09-13'])
    pages = json.loads(subprocess.check_output(['gh', 'api', '--paginate', '--slurp',
                       f'repos/jeffbking/{repo}/actions/runners'], text=True))
    for page in pages:
        for old in page['runners']:
            if old['name'].startswith(label + '-') and old['status'] == 'offline' and not old['busy']:
                run(['gh', 'api', '-X', 'DELETE',
                     f'repos/jeffbking/{repo}/actions/runners/{old["id"]}'])
    config = {'hostname': name, 'ssh_pwauth': False, 'disable_root': True,
              'users': [{'name': 'runner', 'uid': 1000, 'groups': ['sudo', 'docker'],
                         'sudo': 'ALL=(ALL) NOPASSWD:ALL', 'shell': '/bin/bash',
                         'lock_passwd': True,
                         'ssh_authorized_keys': [(root / 'operator_key.pub').read_text().strip()]}]}
    # Mailslop's CI lints deployment shell scripts. Let cloud-init provide the
    # distro package before registering, including with an older clean image.
    if repo == 'mailslop':
        config['package_update'] = True
        config['packages'] = ['shellcheck']
    (directory / 'user-data').write_text('#cloud-config\n' + json.dumps(config) + '\n')
    (directory / 'meta-data').write_text(f'instance-id: {name}\nlocal-hostname: {name}\n')
    run(['docker', 'run', '--rm', '--entrypoint', 'cloud-localds', *mounts, image,
         '/vm/seed.img', '/vm/user-data', '/vm/meta-data'])
    run(['docker', 'run', '--rm', '--entrypoint', 'qemu-img', *mounts, image,
         'create', '-f', 'qcow2', '-F', 'qcow2', '-b', '/golden.img', '/vm/disk.qcow2', '60G'])
    run(['docker', 'run', '-d', '--name', container, '--network', 'ci-vms',
         '--sysctl', 'net.ipv6.conf.all.disable_ipv6=1',
         '--device', '/dev/kvm', '--group-add', str(pathlib.Path('/dev/kvm').stat().st_gid),
         '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--cpus', '2',
         '--memory', f'{memory+768}m', '--memory-swap', f'{memory+768}m', '--pids-limit', '128',
         '-p', f'127.0.0.1:{port}:2222', *mounts, image,
         '-enable-kvm', '-cpu', 'host', '-smp', '2', '-m', str(memory), '-display', 'none',
         '-device', 'virtio-balloon-pci,free-page-reporting=on',
         '-serial', 'file:/vm/serial.log',
         '-drive', 'file=/vm/disk.qcow2,if=virtio,format=qcow2',
         '-drive', 'file=/vm/seed.img,if=virtio,format=raw,readonly=on',
         '-netdev', 'user,id=net0,ipv6=off,hostfwd=tcp::2222-:22', '-device', 'virtio-net-pci,netdev=net0'])
    await_ssh()
    run(ssh + ['cloud-init status --wait && test ! -e /home/runner/actions-runner/.runner'], timeout=240)
    token = json.loads(subprocess.check_output(['gh', 'api', '-X', 'POST',
                      f'repos/jeffbking/{repo}/actions/runners/registration-token'], text=True))['token']
    command = ('cd /home/runner/actions-runner && IFS= read -r registration_token && '
               './config.sh --unattended --ephemeral --disableupdate '
               f'--url https://github.com/jeffbking/{repo} --token "$registration_token" '
               f'--name {name} --labels {label} --work _work && '
               'unset registration_token && exec ./run.sh')
    run(ssh + [command], input=token + '\n', text=True)
finally:
    # The SSH stream contains only a short-lived registration token. No host
    # credentials, Docker socket, or host directory is exposed to the guest.
    logs = root / 'logs' / name
    logs.mkdir(parents=True, exist_ok=True)
    try:
        with (logs / 'runner-diag.tar').open('wb') as stream:
            subprocess.run(ssh + ['tar cf - -C /home/runner/actions-runner _diag'],
                           stdout=stream, timeout=30)
    except subprocess.TimeoutExpired:
        pass
    subprocess.run(['docker', 'stop', '--timeout', '30', container], stdout=subprocess.DEVNULL)
    subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL)
    if (directory / 'serial.log').exists():
        shutil.copyfile(directory / 'serial.log', logs / 'serial.log')
    shutil.rmtree(directory)
    runners = json.loads(subprocess.check_output(['gh', 'api', '--paginate', '--slurp',
                         f'repos/jeffbking/{repo}/actions/runners'], text=True))
    runners = [runner for page in runners for runner in page['runners']]
    for runner in runners:
        if runner['name'] == name:
            run(['gh', 'api', '-X', 'DELETE', f'repos/jeffbking/{repo}/actions/runners/{runner["id"]}'])
