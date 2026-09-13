# Isolated CI on 5900xt

Migrated jobs select `5900xt-m3u-editor-ci`, registered only inside this repository's
Ubuntu 24.04 KVM guest on 5900xt. The guest has its own Docker daemon, loopback,
filesystem, and service containers. Fixed PostgreSQL ports and `--network host`
refer to the guest, not production. Existing deployment runners remain separate.

## Provisioning

Use standard QEMU and cloud-init; no custom virtualization or Docker daemon is
implemented. The Dockerfile packages Ubuntu's maintained QEMU/cloud-image-utils.
Compose starts a pre-provisioned disk and enforces resource limits. The QEMU
process is non-root, with only `/dev/kvm`, all capabilities dropped, no host
Docker socket, and no filesystem share exposed to the guest.

1. Download `noble-server-cloudimg-amd64.img` from
   <https://cloud-images.ubuntu.com/noble/20260911/> and verify SHA256
   `612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354`.
2. Copy `cloud-config.yaml` to the private VM directory as `user-data`, add the
   operator's SSH public key to `ssh_authorized_keys`, and write `meta-data`
   containing a unique `instance-id` and `local-hostname`. Never copy the private
   key into the image. Build this Dockerfile, then use `cloud-localds` to create
   `seed.img` and `qemu-img create -f qcow2 -F qcow2 -b /base.img disk.qcow2 60G`
   with the base image mounted at `/base.img`. Both tools are in the image.
3. Create the external `ci-vms` Docker network with an unused subnet outside
   guest Docker's address pools (5900xt uses `10.89.0.0/24`). Do not use the host's
   default `172.17.0.0/16` bridge: guest Docker would route forwarded SSH replies
   to its own bridge instead of QEMU's gateway.
4. Set `CI_VM_DIRECTORY`, `CI_VM_BASE_IMAGE`, and `KVM_GID` (numeric group from
   `stat -c %g /dev/kvm`), then run `docker compose up -d --build`.
5. SSH to `runner@127.0.0.1` on port `22207` using the operator key. Wait for
   `cloud-init status --wait` to finish, then reconnect so newly granted Docker
   group membership applies. Verify `docker info` and `docker compose version`.
6. Install the official Actions runner in `/home/runner/actions-runner` inside
   the guest. Initial version: 2.337.0, Linux x64 SHA256
   `70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613`.
   Register using a short-lived repository registration token passed through
   SSH stdin: `./config.sh --unattended --url https://github.com/jeffbking/m3u-editor
--token "$registration_token" --name 5900xt-m3u-editor-ci --labels 5900xt-m3u-editor-ci --work _work`.
   Then run `sudo ./svc.sh install runner` and `sudo ./svc.sh start` in that
   directory. Confirm the runner is online with the exact custom label.

The operator key and registration state stay outside git. Do not clone a disk
that contains runner credentials. Automatic runner updates remain enabled;
apply Ubuntu guest updates and refresh pinned base images during maintenance.
The guest is persistent, not recreated for each job. Only this repository's
jobs use it. SSH is published on host loopback only. Monitor available host
memory when adding runners; the per-VM limit is not a fleet-wide quota.
