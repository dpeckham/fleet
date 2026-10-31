"""Presses Tab in real interactive bash and zsh sessions."""

import os
import shutil
import time
from pathlib import Path

import pexpect
import pytest

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = """
[host.box1]
backend = "incus"
address = "x"
[host.mac1]
backend = "lima"
address = "y"
[repo.app]
url = "https://example.com/app.git"
"""


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "fleet.toml"
    config.write_text(INVENTORY)
    return {**os.environ, "FLEET_CONFIG": str(config), "TERM": "dumb",
            "PATH": f"{ROOT / 'bin'}:{os.environ['PATH']}"}


def start(shell, env, tmp_path):
    binary = shutil.which(shell)
    if shell == "bash":
        (tmp_path / ".bashrc").write_text(
            "PS1='READY> '\nbind 'set bell-style none'\nbind 'set show-all-if-ambiguous on'\n"
            "bind 'set completion-query-items 0'\nbind 'set page-completions off'\n"
            'eval "$(fleet completion bash)"\n'
        )
        child = pexpect.spawn(binary, ["--noprofile", "--rcfile", str(tmp_path / ".bashrc"), "-i"],
                              env=env, encoding="utf-8", timeout=15, dimensions=(50, 200))
    else:
        (tmp_path / ".zshrc").write_text(
            "PROMPT='READY> '\nunsetopt BEEP\nautoload -Uz compinit && compinit -u\n"
            "LISTMAX=0\nzstyle ':completion:*' list-prompt ''\n"
            'eval "$(fleet completion zsh)"\n'
        )
        child = pexpect.spawn(binary, ["-i"], env={**env, "ZDOTDIR": str(tmp_path)},
                              encoding="utf-8", timeout=15, dimensions=(50, 200))
    child.expect("READY> ")
    return child


CASES = [
    # (typed text, text that must appear after Tab)
    ("fleet \t", ["attach", "completion", "destroy", "host", "provision", "status", "stop", "up"]),
    ("fleet ho\t", ["host"]),
    ("fleet host \t", ["add", "list"]),
    ("fleet stop --\t", ["--now", "--help", "--ask-become-pass", "--verbose"]),
    ("fleet host add x --\t", ["--ssh", "--backend", "--no-tailscale", "--help"]),
    ("fleet host add x --backend \t", ["incus", "lima"]),
    ("fleet completion \t", ["bash", "zsh"]),
    ("fleet stop \t", ["app@mac1", "app@box1"]),
    ("fleet up app@b\t", ["app@box1"]),
    ("fleet provision \t", ["app"]),
    ("fleet provision app \t", ["mac1", "box1"]),
]


@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("typed,wanted", CASES, ids=[case[0].strip() for case in CASES])
def test_tab(shell, typed, wanted, env, tmp_path):
    child = start(shell, env, tmp_path)
    child.send(typed)
    # Shells list matches in their own order and may first fill in a common prefix (app@),
    # so collect output and press Tab once more before giving up.
    seen = ""
    deadline = time.monotonic() + 15
    pressed_again = False
    while time.monotonic() < deadline and not all(word in seen for word in wanted):
        try:
            seen += child.read_nonblocking(4096, timeout=1)
        except pexpect.TIMEOUT:
            if not pressed_again:
                child.send("\t")
                pressed_again = True
    child.close(force=True)
    assert all(word in seen for word in wanted), f"missing from: {seen!r}"
