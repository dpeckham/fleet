"""Runs playbooks and direct commands on hosts. Argv lists only; nothing goes through a local shell."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from .inventory import FleetError, Host, Repo, env_name, root_dir

RECAP_RE = re.compile(r"ok=\d+\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)")
CHILD_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def ssh_base(host: Host, *, batch: bool = True) -> list[str]:
    """ssh argv up to and including the destination. Never forwards the agent."""
    argv = ["ssh", "-a", "-p", str(host.port)]
    if batch:
        argv += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if host.identity:
        argv += ["-i", host.identity, "-o", "IdentitiesOnly=yes"]
    for option in host.ssh_options:
        argv += ["-o", option]
    destination = f"{host.user}@{host.address}" if host.user else host.address
    return argv + ["--", destination]


def host_argv(host: Host, command: list[str], *, batch: bool = True, tty: bool = False) -> list[str]:
    """Argv that runs `command` on the host."""
    if host.local:
        return command
    base = ssh_base(host, batch=batch)
    if tty:
        base.insert(1, "-t")
    return base + [shlex.join(command)]


def env_exec(host: Host, repo: str, command: list[str]) -> list[str]:
    """Host-side argv that runs `command` as root inside the repo's environment."""
    name = env_name(repo)
    if host.backend == "incus":
        return ["incus", "exec", name, "--", *command]
    return ["limactl", "shell", "--workdir", "/", name, "sudo", "-n", *command]


def env_user_exec(host: Host, repo: str, command: list[str]) -> list[str]:
    """Host-side argv that runs `command` as the dev user in a login shell."""
    return env_exec(host, repo, ["su", "-l", "dev", "-c", shlex.join(command)])


def can_login(host: Host) -> bool:
    if host.local:
        return True
    result = subprocess.run(
        host_argv(host, ["true"]), capture_output=True, text=True, timeout=60, check=False
    )
    return result.returncode == 0


def probe(host: Host, command: list[str], *, stdin: str | None = None, timeout: int = 60):
    """Runs a read-only command on the host and returns the CompletedProcess."""
    return subprocess.run(
        host_argv(host, command),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def host_vars(host: Host) -> dict:
    if host.local:
        return {"ansible_connection": "local", "ansible_python_interpreter": sys.executable}
    args = ["-o", "ForwardAgent=no"]
    for option in host.ssh_options:
        args += ["-o", option]
    variables = {
        "ansible_host": host.address,
        "ansible_port": host.port,
        "ansible_ssh_common_args": shlex.join(args),
        "ansible_python_interpreter": "auto_silent",
    }
    if host.user:
        variables["ansible_user"] = host.user
    if host.identity:
        variables["ansible_ssh_private_key_file"] = host.identity
    return variables


def repo_vars(repo: Repo) -> dict:
    return {
        "repo_name": repo.name,
        "repo_url": repo.url,
        "repo_bootstrap": repo.bootstrap,
        "repo_start": repo.start,
        "repo_stop": repo.stop,
        "repo_stop_now": repo.stop_now,
        "env_name": env_name(repo.name),
    }


def run_playbook(
    playbook: str,
    host: Host,
    variables: dict | None = None,
    *,
    ask_become_pass: bool = False,
    verbose: bool = False,
) -> int:
    """Runs a playbook and returns how many tasks changed something."""
    all_vars = {
        **host_vars(host),
        "backend": host.backend,
        "default_image": host.default_image,
        "lima_autostart": host.lima_autostart,
        **(variables or {}),
    }
    root = root_dir()
    bin_dir = Path(sys.executable).parent
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(root / "ansible" / "ansible.cfg"),
        "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES",
    }
    # Variables may hold secrets, so they go through a private file and never onto argv.
    with tempfile.TemporaryDirectory(prefix="fleet-") as tmp:
        vars_file = Path(tmp) / "vars.json"
        vars_file.touch(mode=0o600)
        vars_file.write_text(json.dumps(all_vars))
        argv = [
            str(bin_dir / "ansible-playbook"),
            "-i", f"{host.name},",
            "-e", f"@{vars_file}",
            str(root / "ansible" / playbook),
        ]
        if ask_become_pass:
            argv.append("--ask-become-pass")
        if verbose:
            argv.append("-v")
        process = subprocess.Popen(
            argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        changed = 0
        failed = True
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            match = RECAP_RE.search(line)
            if match:
                changed = int(match.group(1))
                failed = match.group(2) != "0" or match.group(3) != "0"
        if process.wait() != 0 or failed:
            raise FleetError(f"{playbook} failed on {host.name}; see the output above")
    print(f"fleet: {host.name}: {changed} change{'' if changed == 1 else 's'}")
    return changed


__all__ = ["asdict"]
