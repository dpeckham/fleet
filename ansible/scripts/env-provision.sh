# Runs as root in the environment. Prints FLEET_CHANGED if it altered anything.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

missing=
for package in sudo curl git ca-certificates gpg; do
  dpkg -s "$package" >/dev/null 2>&1 || missing="$missing $package"
done
if [ -n "$missing" ]; then
  apt-get update -qq
  # shellcheck disable=SC2086
  apt-get install -y -qq $missing >/dev/null
  echo FLEET_CHANGED
fi

if ! id dev >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash dev
  echo FLEET_CHANGED
fi
if [ ! -f /etc/sudoers.d/fleet-dev ]; then
  echo 'dev ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/fleet-dev
  chmod 0440 /etc/sudoers.d/fleet-dev
  echo FLEET_CHANGED
fi

if ! command -v mise >/dev/null; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://mise.jdx.dev/gpg-key.pub | gpg --batch --yes --dearmor -o /etc/apt/keyrings/mise-archive-keyring.gpg
  arch=$(dpkg --print-architecture)
  echo "deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg arch=$arch] https://mise.jdx.dev/deb stable main" \
    >/etc/apt/sources.list.d/mise.list
  apt-get update -qq
  apt-get install -y -qq mise >/dev/null
  echo FLEET_CHANGED
fi

# Git asks this helper for GitHub credentials, using GH_TOKEN from the secrets file if present.
helper=/usr/local/bin/fleet-git-credential
if [ ! -x "$helper" ]; then
  cat >"$helper" <<'HELPER'
#!/bin/sh
[ "$1" = get ] || exit 0
secrets=$HOME/.config/fleet/secrets.env
[ -r "$secrets" ] || exit 0
token=$(sed -n 's/^GH_TOKEN=//p' "$secrets" | head -n 1)
[ -n "$token" ] || exit 0
echo username=x-access-token
echo "password=$token"
HELPER
  chmod 0755 "$helper"
  echo FLEET_CHANGED
fi
if [ "$(git config --system --get credential.https://github.com.helper || true)" != "$helper" ]; then
  git config --system credential.https://github.com.helper "$helper"
  echo FLEET_CHANGED
fi
