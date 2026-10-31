"""The fleet command line."""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click
from click.shell_completion import CompletionItem, get_completion_class

from . import runner, tailscale
from .inventory import (
    BACKENDS,
    FleetError,
    Host,
    Inventory,
    check_name,
    env_name,
    parse_target,
    resolve_repo_spec,
    root_dir,
)

CONTEXT = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


def examples(*lines: str) -> str:
    # \b stops Click re-wrapping the lines.
    return "\b\nExamples:\n" + "\n".join(f"  {line}" for line in lines)


def complete_hosts(ctx, param, incomplete):
    try:
        names = Inventory().hosts()
    except Exception:
        return []
    return [CompletionItem(name) for name in sorted(names) if name.startswith(incomplete)]


def complete_repos(ctx, param, incomplete):
    try:
        names = Inventory().repos()
    except Exception:
        return []
    return [CompletionItem(name) for name in sorted(names) if name.startswith(incomplete)]


def complete_targets(ctx, param, incomplete):
    try:
        inventory = Inventory()
        pairs = [f"{r}@{h}" for r in sorted(inventory.repos()) for h in sorted(inventory.hosts())]
    except Exception:
        return []
    return [CompletionItem(pair) for pair in pairs if pair.startswith(incomplete)]


class FleetGroup(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except FleetError as error:
            click.secho(f"fleet: {error}", fg="red", err=True)
            ctx.exit(1)


@click.group(
    cls=FleetGroup,
    context_settings=CONTEXT,
    epilog=examples(
        "fleet host add box1 --ssh me@192.0.2.10 --backend incus",
        "fleet provision me/app box1 --secrets app.secrets.env",
        "fleet up app@box1",
        "fleet status",
        "fleet stop app@box1 --now",
    ),
)
def cli():
    """Provision environments on your machines and run agent pools in them.

    Every command except destroy can be repeated safely: it checks real state and only
    makes up the difference. Machines and repos are listed in fleet.toml; set FLEET_CONFIG
    to use another file.
    """


@cli.group(
    context_settings=CONTEXT,
    epilog=examples("fleet host add box1 --ssh me@192.0.2.10 --backend incus", "fleet host list"),
)
def host():
    """Add and list machines."""


@host.command(
    "add",
    context_settings=CONTEXT,
    epilog=examples(
        "fleet host add box1 --ssh me@192.0.2.10 --backend incus",
        "fleet host add mac1 --ssh me@mac1.local --backend lima --ask-become-pass",
        "fleet host add lab --ssh me@192.0.2.20:2222 --backend incus --no-tailscale",
    ),
)
@click.argument("name")
@click.option("--ssh", "ssh_target", required=True, metavar="USER@ADDRESS[:PORT]",
              help="How to reach the machine now.")
@click.option("--backend", type=click.Choice(BACKENDS), required=True,
              help="Runtime for environments: incus for Debian/Ubuntu, lima for macOS.")
@click.option("--identity", type=click.Path(dir_okay=False), default="",
              help="Private key to log in with. Default: your SSH configuration.")
@click.option("--ssh-option", "ssh_options", multiple=True, metavar="KEY=VALUE",
              help="Extra ssh -o option to keep for this host. Repeatable.")
@click.option("--no-tailscale", is_flag=True, help="Keep using this address; don't join the tailnet.")
@click.option("--keep-password-login", is_flag=True, help="Don't turn off SSH password login.")
@click.option("--ask-become-pass", "-K", is_flag=True, help="Prompt for the host's sudo password.")
@click.option("--verbose", "-v", is_flag=True, help="Show more Ansible detail.")
def host_add(name, ssh_target, backend, identity, ssh_options, no_tailscale, keep_password_login,
             ask_become_pass, verbose):
    """Set up a machine: SSH key, runtime and Tailscale.

    Your first login may ask you to confirm the host key and enter a password once. SSH must
    already be enabled on the machine. Joining the tailnet needs TAILSCALE_API_KEY.
    """
    check_name("host", name)
    for option in ssh_options:
        if "=" not in option or option.startswith("-"):
            raise FleetError(f"invalid --ssh-option {option!r}: expected KEY=VALUE")
    inventory = Inventory()
    host_entry = parse_ssh_target(name, ssh_target, backend, identity, list(ssh_options))
    existing = inventory.hosts().get(name)
    if existing:
        host_entry.default_image = existing.default_image
        host_entry.lima_autostart = existing.lima_autostart
        # Already moved to the tailnet: keep using it rather than the first-contact address.
        if existing.address != host_entry.address and runner.can_login(existing):
            host_entry.address, host_entry.port = existing.address, existing.port

    ensure_key_login(host_entry)
    inventory.set_host(host_entry)
    runner.run_playbook(
        "host.yml",
        host_entry,
        {"harden_ssh": not keep_password_login, "install_tailscale": not no_tailscale},
        ask_become_pass=ask_become_pass,
        verbose=verbose,
    )
    if not no_tailscale:
        join_tailnet(inventory, host_entry, ask_become_pass, verbose)
    click.echo(f"fleet: host {name} is ready at {host_entry.address}")


def parse_ssh_target(name, target, backend, identity, ssh_options) -> Host:
    user, separator, rest = target.rpartition("@")
    address, colon, port_text = rest.partition(":")
    if not address or address.startswith("-") or user.startswith("-") or (separator and not user):
        raise FleetError(f"invalid --ssh value {target!r}: expected USER@ADDRESS[:PORT]")
    if colon and not port_text.isdigit():
        raise FleetError(f"invalid port in --ssh value {target!r}")
    return Host(name=name, backend=backend, address=address, user=user,
                port=int(port_text) if colon else 22, identity=identity, ssh_options=ssh_options)


def ensure_key_login(host_entry: Host) -> None:
    if runner.can_login(host_entry):
        return
    public = public_key_path(host_entry)
    click.echo(f"fleet: installing {public} on {host_entry.address}; you may be asked to confirm "
               "the host key and enter a password once.")
    argv = ["ssh-copy-id", "-i", str(public), "-p", str(host_entry.port)]
    for option in host_entry.ssh_options:
        argv += ["-o", option]
    destination = f"{host_entry.user}@{host_entry.address}" if host_entry.user else host_entry.address
    if subprocess.run(argv + [destination], check=False).returncode != 0:
        raise FleetError(f"could not install an SSH key on {host_entry.address}")
    if not runner.can_login(host_entry):
        raise FleetError(f"key login to {host_entry.address} still fails; password login was left on")


def public_key_path(host_entry: Host) -> Path:
    candidates = (
        [Path(host_entry.identity + ".pub")]
        if host_entry.identity
        else [Path.home() / ".ssh" / f"id_{kind}.pub" for kind in ("ed25519", "ecdsa", "rsa")]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FleetError("no SSH public key found; create one with ssh-keygen -t ed25519")


def join_tailnet(inventory: Inventory, host_entry: Host, ask_become_pass: bool, verbose: bool) -> None:
    joined, dns_name = tailscale.parse_status(
        runner.probe(host_entry, ["sh", "-c", tailscale.CLI_PROBE]).stdout
    )
    if not joined:
        # Only create a key when one is needed, so repeat runs leave no unused keys behind.
        runner.run_playbook(
            "tailscale-join.yml",
            host_entry,
            {"tailscale_auth_key": tailscale.create_auth_key(host_entry.name)},
            ask_become_pass=ask_become_pass,
            verbose=verbose,
        )
        joined, dns_name = tailscale.parse_status(
            runner.probe(host_entry, ["sh", "-c", tailscale.CLI_PROBE]).stdout
        )
    if not joined or not dns_name:
        raise FleetError(f"{host_entry.name} did not join the tailnet")
    if host_entry.address == dns_name:
        return
    candidate = Host(**{**host_entry.__dict__, "address": dns_name, "port": 22})
    if not runner.can_login(candidate):
        raise FleetError(
            f"{host_entry.name} joined as {dns_name}, but this laptop can't reach it there. "
            f"Check Tailscale is running here and your grants allow it; still using {host_entry.address}."
        )
    host_entry.address, host_entry.port = candidate.address, candidate.port
    inventory.set_host(host_entry)


@host.command("list", context_settings=CONTEXT, epilog=examples("fleet host list"))
def host_list():
    """List known machines."""
    hosts = Inventory().hosts()
    if not hosts:
        click.echo("No hosts yet. Add one with 'fleet host add'.")
    for entry in hosts.values():
        click.echo(f"{entry.name}\t{entry.backend}\t{entry.address}")


@cli.command(
    context_settings=CONTEXT,
    epilog=examples(
        "fleet provision me/app box1",
        "fleet provision app box1 --secrets app.secrets.env",
        "fleet provision https://git.example.com/me/app.git mac1 --image debian-13",
    ),
)
@click.argument("repo", shell_complete=complete_repos)
@click.argument("host_name", metavar="HOST", shell_complete=complete_hosts)
@click.option("--image", default=None, help="Image for a new environment, e.g. debian-13.")
@click.option("--secrets", type=click.Path(exists=True, dir_okay=False), default=None,
              help="KEY=VALUE file to install privately in the environment, e.g. with GH_TOKEN.")
@click.option("--ask-become-pass", "-K", is_flag=True, help="Prompt for the host's sudo password.")
@click.option("--verbose", "-v", is_flag=True, help="Show more Ansible detail.")
def provision(repo, host_name, image, secrets, ask_become_pass, verbose):
    """Create REPO's environment on HOST and check the repo out.

    REPO is OWNER/NAME or a git URL the first time, then just NAME. Running this again fills in
    anything missing. It fetches but never resets a checkout, so agents' work is safe.
    """
    inventory = Inventory()
    host_entry = inventory.host(host_name)
    name, url = resolve_repo_spec(repo)
    if url:
        inventory.set_repo_url(name, url)
    repo_entry = inventory.repo(name)
    variables = runner.repo_vars(repo_entry)
    if image:
        variables["image"] = image
    if secrets:
        path = Path(secrets)
        # Only regular files can be left readable; `--secrets <(op inject ...)` is a private pipe.
        mode = path.stat().st_mode
        if stat.S_ISREG(mode) and mode & 0o077:
            raise FleetError(f"{secrets} is readable by other users; run chmod 600 on it")
        variables["secrets_b64"] = base64.b64encode(path.read_bytes()).decode()
    runner.run_playbook("provision.yml", host_entry, variables,
                        ask_become_pass=ask_become_pass, verbose=verbose)


@cli.command(
    context_settings=CONTEXT,
    epilog=examples("fleet up app@box1"),
)
@click.argument("target", metavar="REPO@HOST", shell_complete=complete_targets)
@click.option("--ask-become-pass", "-K", is_flag=True, help="Prompt for the host's sudo password.")
@click.option("--verbose", "-v", is_flag=True, help="Show more Ansible detail.")
def up(target, ask_become_pass, verbose):
    """Start the repo's agents, and keep them running across reboots.

    Runs the repo's start command, by default using config/agents.conf.
    """
    host_entry, repo_entry = resolve_target(target)
    runner.run_playbook("up.yml", host_entry, runner.repo_vars(repo_entry),
                        ask_become_pass=ask_become_pass, verbose=verbose)


@cli.command(
    context_settings=CONTEXT,
    epilog=examples("fleet stop app@box1", "fleet stop app@box1 --now"),
)
@click.argument("target", metavar="REPO@HOST", shell_complete=complete_targets)
@click.option("--now", is_flag=True, help="Terminate running jobs instead of letting them finish.")
@click.option("--ask-become-pass", "-K", is_flag=True, help="Prompt for the host's sudo password.")
@click.option("--verbose", "-v", is_flag=True, help="Show more Ansible detail.")
def stop(target, now, ask_become_pass, verbose):
    """Stop the repo's agents until the next 'fleet up'.

    By default current work finishes. With --now, jobs are terminated: the repo's stop command
    runs first, then anything left is killed. The environment and its files are kept.
    """
    host_entry, repo_entry = resolve_target(target)
    runner.run_playbook("stop.yml", host_entry, {**runner.repo_vars(repo_entry), "stop_now": now},
                        ask_become_pass=ask_become_pass, verbose=verbose)


def resolve_target(target: str):
    repo_name, host_name = parse_target(target)
    inventory = Inventory()
    return inventory.host(host_name), inventory.repo(repo_name)


@cli.command(
    context_settings=CONTEXT,
    epilog=examples("fleet status", "fleet status --json"),
)
@click.option("--json", "as_json", is_flag=True, help="Print JSON lines for scripts.")
def status(as_json):
    """Show every environment on every machine, read live from the hosts."""
    inventory = Inventory()
    hosts = list(inventory.hosts().values())
    if not hosts:
        click.echo("No hosts yet. Add one with 'fleet host add'.")
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = [row for result in pool.map(host_status, hosts) for row in result]
    if as_json:
        for row in rows:
            click.echo(json.dumps(row))
        return
    click.echo(f"{'HOST':<16}{'ENV':<24}{'INSTANCE':<12}{'AGENTS':<12}PROCESSES")
    for row in rows:
        click.echo(f"{row['host']:<16}{row['env']:<24}{row['instance']:<12}"
                   f"{row['agents']:<12}{row['processes']}")


LIMA_PATH = "PATH=$HOME/.local/bin:$HOME/.local/share/mise/shims:/opt/homebrew/bin:/usr/local/bin:$PATH"


def list_instances(host_entry: Host) -> list[tuple[str, str]]:
    if host_entry.backend == "incus":
        result = runner.probe(host_entry, ["incus", "list", "--format", "json", "--", "^fleet-"])
        result.check_returncode()
        return [(item["name"], item["status"]) for item in json.loads(result.stdout or "[]")]
    result = runner.probe(host_entry, ["sh", "-c", f"{LIMA_PATH}; limactl list --json"])
    result.check_returncode()
    items = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return [(item["name"], item["status"]) for item in items if item["name"].startswith("fleet-")]


def host_status(host_entry: Host) -> list[dict]:
    base = {"host": host_entry.name, "agents": "-", "processes": "-"}
    try:
        instances = list_instances(host_entry)
    except (subprocess.SubprocessError, ValueError, OSError):
        return [{**base, "env": "-", "instance": "unreachable"}]
    if not instances:
        return [{**base, "env": "-", "instance": "no envs"}]
    script = (root_dir() / "ansible" / "scripts" / "env-status.sh").read_text()
    rows = []
    for name, state in instances:
        row = {**base, "env": name, "instance": state.lower()}
        if state == "Running":
            command = (
                ["incus", "exec", name, "--", "bash", "-s"]
                if host_entry.backend == "incus"
                else ["sh", "-c", f'{LIMA_PATH}; exec limactl shell --workdir / {shlex.quote(name)} sudo -n bash -s']
            )
            try:
                result = runner.probe(host_entry, command, stdin=script)
                enabled, active, count = result.stdout.split()
            except (subprocess.SubprocessError, ValueError):
                row["agents"] = "unknown"
            else:
                row["agents"] = "up" if enabled == "enabled" and active == "active" else "stopped"
                row["processes"] = count
        rows.append(row)
    return rows


@cli.command(
    context_settings=CONTEXT,
    epilog=examples(
        "fleet attach app@box1",
        "fleet attach app@box1 -- mise exec -- agents console",
    ),
)
@click.argument("target", metavar="REPO@HOST", shell_complete=complete_targets)
@click.argument("command", nargs=-1, type=click.UNPROCESSED, metavar="[-- COMMAND...]")
def attach(target, command):
    """Open a shell as the dev user in the repo's checkout, or run COMMAND there."""
    host_entry, repo_entry = resolve_target(target)
    inner = "cd " + shlex.quote(f"code/{repo_entry.name}") + " && "
    inner += ("exec " + shlex.join(command)) if command else 'exec "$SHELL" -l'
    name = env_name(repo_entry.name)
    if host_entry.backend == "incus":
        remote = ["incus", "exec", name, "--", "su", "-l", "dev", "-c", inner]
    else:
        remote = ["sh", "-c", f"{LIMA_PATH}; exec limactl shell --workdir / {shlex.quote(name)} "
                              f"sudo -n su -l dev -c {shlex.quote(inner)}"]
    argv = runner.host_argv(host_entry, remote, batch=False, tty=True)
    sys.exit(subprocess.run(argv, check=False).returncode)


@cli.command(
    context_settings=CONTEXT,
    epilog=examples("fleet destroy app@box1", "fleet destroy app@box1 --yes"),
)
@click.argument("target", metavar="REPO@HOST", shell_complete=complete_targets)
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
@click.option("--verbose", "-v", is_flag=True, help="Show more Ansible detail.")
def destroy(target, yes, verbose):
    """Delete the repo's environment, including unpushed work and secrets in it."""
    host_entry, repo_entry = resolve_target(target)
    name = env_name(repo_entry.name)
    if not yes:
        click.confirm(f"Delete {name} on {host_entry.name}, including unpushed work?", abort=True)
    runner.run_playbook("destroy.yml", host_entry, {"env_name": name}, verbose=verbose)


# Bash treats '@' as a word break, which would stop REPO@HOST completing as one word.
BASH_PREAMBLE = "COMP_WORDBREAKS=${COMP_WORDBREAKS//@/}\n"


@cli.command(
    context_settings=CONTEXT,
    epilog=examples(
        'eval "$(fleet completion bash)"    # add to ~/.bashrc',
        'eval "$(fleet completion zsh)"     # add to ~/.zshrc, after compinit',
    ),
)
@click.argument("shell", type=click.Choice(["bash", "zsh"]))
def completion(shell):
    """Print the Tab-completion script for your shell."""
    os.environ.setdefault("LC_ALL", "C.UTF-8")
    completer = get_completion_class(shell)(cli, {}, "fleet", "_FLEET_COMPLETE")
    script = completer.source()
    click.echo(BASH_PREAMBLE + script if shell == "bash" else script)


def main():
    # The bash script Click generates checks its own bash version; nothing else runs for completion.
    if shutil.which("ssh") is None and not os.environ.get("_FLEET_COMPLETE"):
        click.secho("fleet: warning: ssh not found on PATH", fg="yellow", err=True)
    cli()


if __name__ == "__main__":
    main()
