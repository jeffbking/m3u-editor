# Disposable CI VMs on 5900xt

Jobs select `5900xt-m3u-editor-ci`. Each job boots a fresh Ubuntu 24.04 KVM guest from the
read-only `golden.qcow2`, registers with `--ephemeral`, then discards its writable
disk. PR jobs and later privileged jobs cannot inherit filesystem changes.
Docker runs inside the guest: service ports, privileged build containers and
`--network host` refer to that guest. Production deployment runners are separate.
The non-root QEMU process has only /dev/kvm, dropped capabilities, 2 CPUs,
2048 MiB guest RAM and 2816 MiB outer memory limit. Only guest SSH is forwarded,
on host loopback port 22207. No host filesystem or Docker socket reaches the guest.

## CI network policy

Before enabling a runner, build and install the shared network policy from this directory:

```bash
docker network inspect ci-containers >/dev/null 2>&1 || docker network create --subnet 10.90.0.0/24 -o com.docker.network.bridge.enable_icc=false ci-containers
docker network inspect ci-vms >/dev/null 2>&1 || docker network create --subnet 10.89.0.0/24 ci-vms
docker build -t local/ci-runner-firewall:2026-09-13 network
install -Dm644 network/ci-runner-firewall.service "$HOME/.config/systemd/user/ci-runner-firewall.service"
systemctl --user daemon-reload
systemctl --user enable --now ci-runner-firewall.service
```

Both networks are IPv4-only. The host-side helper has NET_ADMIN solely to install
idempotent rules in Docker's DOCKER-USER and host INPUT chains. Only traffic from
10.89.0.0/24 and 10.90.0.0/24 enters these rules. New connections to host services,
private/LAN/tailnet/link-local addresses and other CI guests are rejected; replies
to operator-initiated SSH remain allowed. Public internet access is permitted.
The launcher reapplies the rules before each new job environment, including after
Docker restarts. Job containers never get NET_ADMIN or the helper's host network.
Do not attach other workloads to these reserved networks or enable IPv6 without
an equivalent IPv6 policy. Existing host firewall rules are never flushed.

Verified on 5900xt: GitHub HTTPS succeeds; host gateway SSH and LAN HTTP probes
increment the dedicated REJECT counters. This is network isolation, not an
internet destination allowlist. Docker containers still share the host kernel.

## Build a clean image

Use the authenticated host operator account. Standard QEMU/cloud-init implement
virtualization and guest provisioning. Tenacity 9.1.4 (Apache-2.0, Python >=3.10,
29 kB wheel, no runtime dependencies) supplies bounded SSH readiness retries;
OpenSSH ConnectionAttempts does not retry an accepted connection's early handshake
reset. See <https://tenacity.readthedocs.io/en/stable/>. The launcher contains only
host-specific integration. Keep credentials and disk images outside git.

```bash
install -d -m 700 "$HOME/actions-runners/ci-vms"
install -m 600 run-ephemeral-vm.py prepare-template.py requirements.txt cloud-config.json "$HOME/actions-runners/ci-vms/"
docker build -t local/ci-qemu:2026-09-13 .
cd "$HOME/actions-runners/ci-vms"
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt
# Generate once, only if this operator key does not already exist:
test -e operator_key || ssh-keygen -t ed25519 -N '' -f operator_key
curl -fLO https://cloud-images.ubuntu.com/noble/20260911/noble-server-cloudimg-amd64.img
curl -fLO https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz
.venv/bin/python prepare-template.py
```

The preparation script verifies both SHA256 hashes before booting a template on
port 22209. It installs OS dependencies and the official runner without registering
it, clears cloud-init state/machine-id, shuts down cleanly, and flattens the image.
It refuses to overwrite an existing golden image. The operator public key enters
the guest; the private key stays on the host. For image refresh, build in a separate
directory and switch only after all runner units are stopped while idle.
The host needs Python venv/pip support; on 5900xt the pure-Python Tenacity wheel was
installed into the venv using the existing Ubuntu tooling container's pip.

## Enable this runner

Back in this checked-in directory:

```bash
install -Dm644 ci-vm.service "$HOME/.config/systemd/user/ci-vm-m3u-editor.service"
install -Dm644 logs.conf "$HOME/.config/user-tmpfiles.d/ci-runner-logs.conf"
systemctl --user daemon-reload
systemctl --user enable --now ci-vm-m3u-editor.service
systemctl --user enable --now systemd-tmpfiles-clean.timer
```

The supervisor uses unique instance IDs and per-instance SSH known-host files for
the newly created local guest. It archives diagnostics under
`~/actions-runners/ci-vms/logs/<name>` and deletes the per-job disk. Guest disks are
60 GiB virtual size; host disk capacity and log retention still require monitoring.
The outer Docker subnet must differ from guest Docker's default 172.17/16, or SSH
responses may route into the guest's Docker bridge.

## Maintenance and verification

Stop or replace a runner only after its GitHub registration reports `busy: false`.
A fresh unique runner name is expected after each job, with a short offline gap.
The Runner preflight checks tools and identity without checkout or application
secrets. The host `gh` credential is never copied into a job environment. Only a
short-lived registration token crosses stdin; `config.sh` briefly receives that
token in its guest/container argv and writes credentials inside the disposable
environment. Never run `gh auth login` inside a job environment.

`--disableupdate` prevents updates from being discarded and downloaded again on
every job. Refresh the official runner image/archive at least every 30 days, and
sooner for mandatory/security updates, then rebuild before GitHub stops accepting
jobs. OS packages follow Ubuntu security updates at build time; these builds are
not bit-for-bit reproducible. Record `docker image inspect <image> --format
'{{.Id}}'` with maintenance records. Rebuild explicitly rather than reusing an old
image unintentionally. Do not modify a golden image while runners use it.

Ensure `loginctl show-user "$USER" -p Linger` reports yes for reboot startup.
`systemctl --user status <unit>` and `journalctl --user -u <unit> -n 50` expose
registration failures; `gh api repos/jeffbking/m3u-editor/actions/runners --paginate`
checks online registrations. Restarts use systemd backoff, not a marker-file poll.
Archive directories are private to the operator; apply the supplied tmpfiles
policy to retain seven days of diagnostics. This host has finite capacity: watch
available RAM and memory pressure when changing concurrency or per-job limits.

GitHub contract: <https://docs.github.com/en/actions/reference/runners/self-hosted-runners#ephemeral-runners-for-autoscaling>.
