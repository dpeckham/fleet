"""fleet.toml: what machines and repos exist. Live state is always read from the hosts."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,48}$")
BACKENDS = ("incus", "lima")
DEFAULT_BOOTSTRAP = "mise trust && mise install"
DEFAULT_START = "mise exec -- agents --conf=config/agents.conf"
DEFAULT_STOP = "mise exec -- agents stop"
DEFAULT_STOP_NOW = "mise exec -- agents stop --now"


class FleetError(Exception):
    """An error to show the user without a traceback."""


def check_name(kind: str, name: str) -> str:
    if not NAME_RE.fullmatch(name):
        raise FleetError(f"invalid {kind} name {name!r}: use letters, digits, '-' and '_'")
    return name


def root_dir() -> Path:
    return Path(os.environ.get("FLEET_ROOT", Path(__file__).resolve().parents[2]))


def config_path() -> Path:
    return Path(os.environ.get("FLEET_CONFIG", root_dir() / "fleet.toml"))


@dataclass
class Host:
    name: str
    backend: str
    address: str
    user: str = ""
    port: int = 22
    identity: str = ""
    ssh_options: list[str] = field(default_factory=list)
    default_image: str = "debian-13"
    lima_autostart: str = "boot"

    @property
    def local(self) -> bool:
        return self.address == "local"


@dataclass
class Repo:
    name: str
    url: str
    bootstrap: str = DEFAULT_BOOTSTRAP
    start: str = DEFAULT_START
    stop: str = DEFAULT_STOP
    stop_now: str = DEFAULT_STOP_NOW


def env_name(repo: str) -> str:
    """The instance name; Incus and Lima both accept this form."""
    return "fleet-" + repo.lower().replace("_", "-")


class Inventory:
    def __init__(self, path: Path | None = None):
        self.path = path or config_path()
        if self.path.exists():
            self.doc = tomlkit.parse(self.path.read_text())
        else:
            self.doc = tomlkit.document()

    def hosts(self) -> dict[str, Host]:
        hosts = {}
        for name, data in self.doc.get("host", {}).items():
            hosts[name] = Host(name=check_name("host", name), **_plain(data))
        return hosts

    def repos(self) -> dict[str, Repo]:
        repos = {}
        for name, data in self.doc.get("repo", {}).items():
            repos[name] = Repo(name=check_name("repo", name), **_plain(data))
        return repos

    def host(self, name: str) -> Host:
        hosts = self.hosts()
        if name not in hosts:
            known = ", ".join(sorted(hosts)) or "none yet"
            raise FleetError(f"unknown host {name!r} (known: {known}); run 'fleet host add' first")
        return hosts[name]

    def repo(self, name: str) -> Repo:
        repos = self.repos()
        if name not in repos:
            known = ", ".join(sorted(repos)) or "none yet"
            raise FleetError(
                f"unknown repo {name!r} (known: {known}); "
                "use 'fleet provision OWNER/REPO HOST' the first time"
            )
        return repos[name]

    def set_host(self, host: Host) -> None:
        values = {
            "backend": host.backend,
            "address": host.address,
            "user": host.user,
            "port": host.port,
            "identity": host.identity,
            "ssh_options": host.ssh_options,
            "default_image": host.default_image,
            "lima_autostart": host.lima_autostart,
        }
        defaults = Host(name="", backend="", address="")
        self._merge("host", host.name, {
            key: value for key, value in values.items()
            if key in ("backend", "address") or value != getattr(defaults, key)
        })

    def set_repo_url(self, name: str, url: str) -> None:
        self._merge("repo", name, {"url": url})

    def _merge(self, section: str, name: str, values: dict) -> None:
        if section not in self.doc:
            self.doc[section] = tomlkit.table(is_super_table=True)
        if name not in self.doc[section]:
            self.doc[section][name] = tomlkit.table()
        table = self.doc[section][name]
        changed = False
        for key, value in values.items():
            if table.get(key) != value:
                table[key] = value
                changed = True
        if changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(tomlkit.dumps(self.doc))


def _plain(data) -> dict:
    return data.unwrap() if hasattr(data, "unwrap") else dict(data)


def parse_target(target: str) -> tuple[str, str]:
    repo, separator, host = target.partition("@")
    if not separator or not repo or not host:
        raise FleetError(f"invalid target {target!r}: expected REPO@HOST, e.g. app@box1")
    return check_name("repo", repo), check_name("host", host)


def resolve_repo_spec(spec: str) -> tuple[str, str | None]:
    """Turn NAME, OWNER/NAME or a URL into (name, url); url is None for a known bare name."""
    if "://" in spec or spec.startswith("git@"):
        name = spec.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
        return check_name("repo", name), spec
    if "/" in spec:
        owner, _, name = spec.partition("/")
        check_name("repo owner", owner)
        check_name("repo", name)
        return name, f"https://github.com/{owner}/{name}.git"
    return check_name("repo", spec), None
