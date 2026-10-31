import base64
import http.server
import json
import os
import threading

import click
import pytest
from click.testing import CliRunner

from fleet import cli as cli_module
from fleet import tailscale
from fleet.cli import cli, parse_ssh_target
from fleet.inventory import FleetError, Inventory, Host, env_name, parse_target, resolve_repo_spec


def all_commands(group=cli, prefix=()):
    for name, command in group.commands.items():
        yield (*prefix, name), command
        if isinstance(command, click.Group):
            yield from all_commands(command, (*prefix, name))


@pytest.mark.parametrize("path", [()] + [path for path, _ in all_commands()], ids=" ".join)
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_every_command_prints_usage_and_examples(path, flag):
    result = CliRunner().invoke(cli, [*path, flag])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "Examples:" in result.output
    # Examples must be real invocations of this command.
    assert f"fleet {' '.join(path)}".strip() in result.output


def test_bare_groups_show_help():
    assert "Usage:" in CliRunner().invoke(cli, []).output
    assert "Usage:" in CliRunner().invoke(cli, ["host"]).output


@pytest.mark.parametrize("target", ["app", "@h", "r@", "a b@h", "r@h;rm -rf", "-x@h", "r@../h"])
def test_bad_targets_are_rejected(target):
    with pytest.raises(FleetError):
        parse_target(target)


def test_repo_specs():
    assert resolve_repo_spec("app") == ("app", None)
    assert resolve_repo_spec("me/app") == ("app", "https://github.com/me/app.git")
    assert resolve_repo_spec("git@github.com:me/app.git")[0] == "app"
    assert resolve_repo_spec("https://x.org/me/app.git") == ("app", "https://x.org/me/app.git")
    assert env_name("My_App") == "fleet-my-app"


@pytest.mark.parametrize("value", ["-oProxyCommand=x", "u@-oProxyCommand=x", "@host", "u@h:port"])
def test_ssh_target_rejects_option_injection(value):
    with pytest.raises(FleetError):
        parse_ssh_target("h", value, "incus", "", [])


def test_inventory_round_trip_keeps_comments_and_is_stable(tmp_path):
    path = tmp_path / "fleet.toml"
    path.write_text("# mine\n")
    inventory = Inventory(path)
    entry = Host(name="a", backend="incus", address="1.2.3.4", user="me")
    inventory.set_host(entry)
    first = path.read_text()
    assert "# mine" in first
    Inventory(path).set_host(entry)
    assert path.read_text() == first
    assert Inventory(path).host("a") == entry


def test_provision_rejects_world_readable_secrets(tmp_path, monkeypatch):
    config = tmp_path / "fleet.toml"
    config.write_text('[host.a]\nbackend="incus"\naddress="x"\n')
    secrets = tmp_path / "s.env"
    secrets.write_text("GH_TOKEN=x\n")
    secrets.chmod(0o644)
    monkeypatch.setenv("FLEET_CONFIG", str(config))
    monkeypatch.setattr(cli_module.runner, "run_playbook", lambda *a, **k: pytest.fail("ran"))
    result = CliRunner().invoke(cli, ["provision", "me/r", "a", "--secrets", str(secrets)])
    assert result.exit_code == 1 and "chmod 600" in result.output


def test_provision_accepts_secrets_from_a_pipe(tmp_path, monkeypatch):
    config = tmp_path / "fleet.toml"
    config.write_text('[host.a]\nbackend="incus"\naddress="x"\n')
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo, 0o640)
    threading.Thread(target=lambda: fifo.write_text("GH_TOKEN=x\n"), daemon=True).start()
    monkeypatch.setenv("FLEET_CONFIG", str(config))
    seen = {}
    monkeypatch.setattr(cli_module.runner, "run_playbook",
                        lambda name, host, variables, **kwargs: seen.update(variables))
    result = CliRunner().invoke(cli, ["provision", "me/r", "a", "--secrets", str(fifo)])
    assert result.exit_code == 0, result.output
    assert base64.b64decode(seen["secrets_b64"]) == b"GH_TOKEN=x\n"


def test_status_output_parsing():
    running = json.dumps({"BackendState": "Running", "Self": {"DNSName": "h.tail.ts.net."}})
    assert tailscale.parse_status(running) == (True, "h.tail.ts.net")
    assert tailscale.parse_status('{"BackendState":"NeedsLogin"}')[0] is False
    assert tailscale.parse_status("")[0] is False


class KeyApi(http.server.BaseHTTPRequestHandler):
    seen = {}
    status = 200

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        KeyApi.seen = {"path": self.path, "auth": self.headers["Authorization"], "body": body}
        self.send_response(KeyApi.status)
        self.end_headers()
        self.wfile.write(b'{"key":"tskey-auth-x"}' if KeyApi.status == 200 else b'{"message":"nope"}')

    def log_message(self, *args):
        pass


@pytest.fixture
def key_api():
    server = http.server.HTTPServer(("127.0.0.1", 0), KeyApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    KeyApi.status = 200
    yield f"http://127.0.0.1:{server.server_port}/api/v2"
    server.shutdown()


def test_auth_key_is_single_use_short_lived_and_tagged(key_api, monkeypatch):
    monkeypatch.setenv("TAILSCALE_API_KEY", "tok")
    assert tailscale.create_auth_key("box1", api_base=key_api) == "tskey-auth-x"
    create = KeyApi.seen["body"]["capabilities"]["devices"]["create"]
    assert KeyApi.seen["path"] == "/api/v2/tailnet/-/keys"
    assert KeyApi.seen["auth"] == "Bearer tok"
    assert create == {"reusable": False, "ephemeral": False, "preauthorized": True,
                      "tags": ["tag:fleet-host"]}
    assert KeyApi.seen["body"]["expirySeconds"] == 600


def test_auth_key_errors_are_clear(key_api, monkeypatch):
    monkeypatch.delenv("TAILSCALE_API_KEY", raising=False)
    with pytest.raises(FleetError, match="TAILSCALE_API_KEY"):
        tailscale.create_auth_key("box1", api_base=key_api)
    monkeypatch.setenv("TAILSCALE_API_KEY", "tok")
    KeyApi.status = 400
    with pytest.raises(FleetError, match="tagOwners"):
        tailscale.create_auth_key("box1", api_base=key_api)


def test_key_install_is_skipped_when_login_works(monkeypatch):
    monkeypatch.setattr(cli_module.runner, "can_login", lambda host: True)
    monkeypatch.setattr(cli_module.subprocess, "run", lambda *a, **k: pytest.fail("ssh-copy-id ran"))
    cli_module.ensure_key_login(Host(name="a", backend="incus", address="x"))
