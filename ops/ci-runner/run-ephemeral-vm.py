#!/usr/bin/env python3
"""One disposable QEMU guest per Actions job; systemd owns restart/backoff."""
import json
import pathlib
import re
import resource
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
repo = repo.lower()
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
    kwargs.setdefault('timeout', 120)
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
network = json.loads(subprocess.check_output(['docker', 'network', 'inspect', 'ci-vms'], text=True, timeout=30))[0]
if (network['Driver'] != 'bridge' or network['EnableIPv6'] or
        [entry.get('Subnet') for entry in network['IPAM']['Config']] != ['10.89.0.0/24']):
    raise SystemExit('Dedicated CI network configuration does not match firewall policy')

try:
    # The service is a singleton. Preserve every mounted directory, including
    # stopped containers, and reclaim only this repository's abandoned state.
    mounted = set()
    ids = subprocess.check_output(['docker', 'ps', '-aq'], text=True, timeout=30).split()
    if ids:
        for line in subprocess.check_output(['docker', 'inspect', '--format', '{{json .Mounts}}', *ids], text=True, timeout=30).splitlines():
            mounted.update(PathMount['Source'] for PathMount in json.loads(line))
    for old in directory.parent.glob(label + '-*'):
        if old != directory and str(old) not in mounted and old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
    if shutil.disk_usage(root).free < 80 * 1024**3:
        raise SystemExit('Refusing to boot: CI volume needs at least 80 GiB free')
    run(['docker', 'run', '--pull=never', '--rm', '--network', 'host', '--cap-drop', 'ALL',
         '--cap-add', 'NET_ADMIN', '--security-opt', 'no-new-privileges',
         'local/ci-runner-firewall:2026-09-13'])
    pages = json.loads(subprocess.check_output(['gh', 'api', '--paginate', '--slurp',
                       f'repos/jeffbking/{repo}/actions/runners'], text=True, timeout=60))
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
    run(['docker', 'run', '--pull=never', '--rm', '--entrypoint', 'cloud-localds', *mounts, image,
         '/vm/seed.img', '/vm/user-data', '/vm/meta-data'])
    run(['docker', 'run', '--pull=never', '--rm', '--entrypoint', 'qemu-img', *mounts, image,
         'create', '-f', 'qcow2', '-F', 'qcow2', '-b', '/golden.img', '/vm/disk.qcow2', '60G'])
    run(['docker', 'run', '--pull=never', '-d', '--name', container, '--network', 'ci-vms',
         '--log-driver', 'local', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
         '--sysctl', 'net.ipv6.conf.all.disable_ipv6=1',
         '--device', '/dev/kvm', '--group-add', str(pathlib.Path('/dev/kvm').stat().st_gid),
         '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--cpus', '2',
         '--memory', f'{memory+768}m', '--memory-swap', f'{memory+768}m', '--pids-limit', '128',
         '-p', f'127.0.0.1:{port}:2222', *mounts, image,
         '-enable-kvm', '-cpu', 'host', '-smp', '2', '-m', str(memory), '-display', 'none',
         '-device', 'virtio-balloon-pci,free-page-reporting=on',
         '-serial', 'stdio', '-monitor', 'none',
         '-drive', 'file=/vm/disk.qcow2,if=virtio,format=qcow2',
         '-drive', 'file=/vm/seed.img,if=virtio,format=raw,readonly=on',
         '-netdev', 'user,id=net0,ipv6=off,hostfwd=tcp::2222-:22', '-device', 'virtio-net-pci,netdev=net0'])
    await_ssh()
    run(ssh + ['cloud-init status --wait && test ! -e /home/runner/actions-runner/.runner'], timeout=240)
    token = json.loads(subprocess.check_output(['gh', 'api', '-X', 'POST',
                      f'repos/jeffbking/{repo}/actions/runners/registration-token'], text=True, timeout=60))['token']
    command = ('cd /home/runner/actions-runner && IFS= read -r registration_token && '
               './config.sh --unattended --ephemeral --disableupdate '
               f'--url https://github.com/jeffbking/{repo} --token "$registration_token" '
               f'--name {name} --labels {label} --work _work && '
               'unset registration_token && exec ./run.sh')
    run(ssh + [command], input=token + '\n', text=True, timeout=None)
finally:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    # Linux enforces the archive byte limit in the child that writes the file.
    # This also bounds a guest that continuously generates diagnostic output.
    def limit_archive():
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))

    # The SSH stream contains only a short-lived registration token. No host
    # credentials, Docker socket, or host directory is exposed to the guest.
    logs = root / 'logs' / name
    try:
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / 'runner-diag.tar').open('wb') as stream:
            subprocess.run(ssh + ['tar cf - -C /home/runner/actions-runner _diag'],
                           stdout=stream, timeout=30, preexec_fn=limit_archive)
    except (OSError, subprocess.SubprocessError) as error:
        print(f'Runner diagnostic archive failed: {error}', file=sys.stderr)
    try:
        with (logs / 'serial.log').open('wb') as stream:
            subprocess.run(['docker', 'logs', '--tail', '1000', container],
                           stdout=stream, stderr=subprocess.STDOUT, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        print(f'Diagnostic archive failed: {error}', file=sys.stderr)
    removed = False
    for command in [['docker', 'stop', '--timeout', '30', container], ['docker', 'rm', '-f', container]]:
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, timeout=40)
            if command[1] == 'rm':
                removed = result.returncode == 0
        except (OSError, subprocess.SubprocessError) as error:
            print(f'Container cleanup failed: {error}', file=sys.stderr)
    if not removed:
        print('Container removal unconfirmed; retaining disk and registration for recovery', file=sys.stderr)
    else:
        try:
            shutil.rmtree(directory)
        except OSError as error:
            print(f'Instance directory cleanup failed: {error}', file=sys.stderr)
        try:
            runners = json.loads(subprocess.check_output(['gh', 'api', '--paginate', '--slurp',
                                 f'repos/jeffbking/{repo}/actions/runners'], text=True, timeout=60))
            for page in runners:
                for runner in page['runners']:
                    if runner['name'] == name:
                        run(['gh', 'api', '-X', 'DELETE', f'repos/jeffbking/{repo}/actions/runners/{runner["id"]}'])
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            # Startup retries cleanup of offline owned names after API recovery.
            # Do not mask the original job/SSH failure with a cleanup exception.
            print(f'Registration cleanup failed: {error}', file=sys.stderr)
