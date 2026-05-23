"""Tiny helper that runs a remote command via paramiko and prints the
result. Handles auto-accept of unknown host keys and streams output.

Usage:
    python scripts/deploy/ssh_runner.py 'remote command here'

Connection params come from env so the password is not in the
process argv:
    PLANEAI_DEPLOY_HOST=79.137.198.99
    PLANEAI_DEPLOY_USER=root
    PLANEAI_DEPLOY_PASS=...
"""

from __future__ import annotations

import os
import sys

import paramiko


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ssh_runner.py '<command>'", file=sys.stderr)
        return 2
    cmd = sys.argv[1]

    host = os.environ.get("PLANEAI_DEPLOY_HOST")
    user = os.environ.get("PLANEAI_DEPLOY_USER", "root")
    password = os.environ.get("PLANEAI_DEPLOY_PASS")
    if not host or not password:
        print(
            "set PLANEAI_DEPLOY_HOST and PLANEAI_DEPLOY_PASS env vars",
            file=sys.stderr,
        )
        return 2

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=user,
        password=password,
        look_for_keys=False,
        allow_agent=False,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
    )

    # Use exec_command with stderr merged so output ordering matches a real shell
    chan = client.get_transport().open_session()
    chan.get_pty()
    chan.exec_command(cmd)

    while True:
        if chan.recv_ready():
            data = chan.recv(4096)
            if not data:
                break
            sys.stdout.write(data.decode(errors="replace"))
            sys.stdout.flush()
        if chan.exit_status_ready() and not chan.recv_ready():
            break

    # Drain anything left
    while chan.recv_ready():
        data = chan.recv(4096)
        sys.stdout.write(data.decode(errors="replace"))
        sys.stdout.flush()

    exit_status = chan.recv_exit_status()
    client.close()
    sys.stdout.write(f"\n[exit={exit_status}]\n")
    return exit_status


if __name__ == "__main__":
    sys.exit(main())
