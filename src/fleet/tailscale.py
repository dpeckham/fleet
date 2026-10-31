"""Tailscale: read a host's state and create single-use auth keys."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .inventory import FleetError

API_BASE = "https://api.tailscale.com/api/v2"
FLEET_TAG = "tag:fleet-host"
CLI_PROBE = (
    "PATH=$PATH:/usr/local/bin:/opt/homebrew/bin:/Applications/Tailscale.app/Contents/MacOS; "
    "if command -v tailscale >/dev/null; then tailscale status --json; "
    "elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then "
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale status --json; fi"
)


def parse_status(text: str) -> tuple[bool, str]:
    """Returns (joined, dns_name) from `tailscale status --json` output."""
    try:
        status = json.loads(text)
    except ValueError:
        return False, ""
    joined = status.get("BackendState") == "Running"
    dns_name = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    return joined, dns_name


def create_auth_key(host_name: str, *, api_base: str | None = None) -> str:
    """Creates a one-time, pre-approved, tagged key valid for ten minutes."""
    token = os.environ.get("TAILSCALE_API_KEY", "")
    if not token:
        raise FleetError(
            "TAILSCALE_API_KEY is not set. Create an API access token in the Tailscale admin "
            "console and export it, or use --no-tailscale to skip joining the tailnet."
        )
    body = {
        "keyType": "auth",
        "description": f"fleet {host_name}"[:50],
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": False,
                    "ephemeral": False,
                    "preauthorized": True,
                    "tags": [FLEET_TAG],
                }
            }
        },
        "expirySeconds": 600,
    }
    request = urllib.request.Request(
        f"{api_base or API_BASE}/tailnet/-/keys",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)["key"]
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        hint = ""
        if FLEET_TAG in detail or error.code == 400:
            hint = f" Check that {FLEET_TAG} is declared in tagOwners in your tailnet policy."
        raise FleetError(f"Tailscale API returned {error.code}: {detail}.{hint}") from error
    except urllib.error.URLError as error:
        raise FleetError(f"could not reach the Tailscale API: {error.reason}") from error
