#!/usr/bin/env bash
# Real end-to-end run in throwaway VMs; touches nothing else.
#  1. A Debian VM acts as a remote host: `host add` over SSH, Incus backend.
#  2. This laptop acts as a local macOS host: Lima backend, with a VM per environment.
# Keep VMs afterwards with FLEET_E2E_KEEP=1.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/fleet-e2e.XXXXXX")
export FLEET_CONFIG=$work/fleet.toml
export PATH=$root/bin:$PATH
hostvm=fleet-e2e-host
daemon_pid=
fail() { echo "E2E FAILED: $*" >&2; exit 1; }
pass() { echo "ok - $*"; }

cleanup() {
  [ -z "$daemon_pid" ] || kill "$daemon_pid" 2>/dev/null || true
  if [ -z "${FLEET_E2E_KEEP:-}" ]; then
    limactl delete --force "$hostvm" fleet-fixture >/dev/null 2>&1 || true
    rm -rf "$work"
  fi
}
trap 'status=$?; [ $status = 0 ] || echo "E2E FAILED: exit $status near line ${BASH_LINENO[0]}" >&2; cleanup' EXIT

changes() { sed -n 's/^fleet: .*: \([0-9]*\) change.*/\1/p' | tail -1; }
# Runs a fleet command, echoing output, and stores the change count in $changed.
run() {
  local log=$work/last.log
  "$@" 2>&1 | tee "$log" || fail "command failed: $*"
  changed=$(changes <"$log")
}
expect_unchanged() { run "$@"; [ "$changed" = 0 ] || fail "second run changed $changed: $*"; pass "idempotent: $*"; }

# The fixture repo, as a bare git repo served read-only.
git init -q -b main "$work/src"
cp -R "$root/tests/fixtures/repo/." "$work/src/"
git -C "$work/src" add -A
git -C "$work/src" -c user.name=t -c user.email=t@t commit -qm fixture
git clone -q --bare "$work/src" "$work/git/fixture.git"
touch "$work/git/fixture.git/git-daemon-export-ok"

echo "== Incus backend, remote host over SSH =="
limactl --tty=false delete --force "$hostvm" >/dev/null 2>&1 || true
limactl --tty=false start --name="$hostvm" --plain --cpus=4 --memory=4 template:debian-13 >/dev/null 2>&1
port=$(limactl list --json "$hostvm" | python3 -c 'import json,sys; print(json.loads(sys.stdin.readline())["sshLocalPort"])')
user=$(limactl shell "$hostvm" whoami)
key=$HOME/.lima/_config/user
ssh_opts=(--ssh-option "UserKnownHostsFile=$work/known_hosts" --ssh-option StrictHostKeyChecking=accept-new)

# Lima's network answers every ping, so Incus's automatic subnet search finds nothing free.
# Only in this test, configure the network first; `host add` then sees Incus is initialized.
limactl shell "$hostvm" sudo sh -c 'apt-get update -qq && apt-get install -y -qq incus dnsmasq-base nftables >/dev/null'
limactl shell "$hostvm" sudo incus admin init --preseed <<'YAML'
networks:
- name: incusbr0
  type: bridge
  config: {ipv4.address: 10.77.0.1/24, ipv4.nat: "true", ipv6.address: none}
storage_pools:
- name: default
  driver: dir
profiles:
- name: default
  devices:
    root: {path: /, pool: default, type: disk}
    eth0: {name: eth0, network: incusbr0, type: nic}
YAML

add=(fleet host add remote --ssh "$user@127.0.0.1:$port" --backend incus --identity "$key" --no-tailscale "${ssh_opts[@]}")
run "${add[@]}"
expect_unchanged "${add[@]}"
limactl shell "$hostvm" sudo sshd -T | grep -qi '^passwordauthentication no' || fail "password login still on"
pass "password login is off"

# Serve the fixture from the host VM, on the address containers can reach.
limactl shell "$hostvm" sudo apt-get install -y -qq git >/dev/null
tar -C "$work/git" -cf - fixture.git | limactl shell "$hostvm" sh -c 'mkdir -p /tmp/git && tar -C /tmp/git -xf -'
gateway=$(limactl shell "$hostvm" sudo incus network get incusbr0 ipv4.address | cut -d/ -f1)
limactl shell "$hostvm" sh -c "nohup git daemon --base-path=/tmp/git --export-all --listen=$gateway --reuseaddr >/dev/null 2>&1 &"
url=git://$gateway/fixture.git

exercise() { # exercise <host>
  local h=$1 target=fixture@$1
  in_env() { fleet attach "$target" -- "$@" </dev/null 2>/dev/null | tr -d '\r'; }
  procs() { fleet status --json | python3 -c "
import json,sys
for line in sys.stdin:
    r=json.loads(line)
    if r['host']=='$h' and r['env']=='fleet-fixture': print(r['agents'], r['processes'])"; }

  printf 'GH_TOKEN=test-token\nFIXTURE_SECRET=s3cret\n' >"$work/x.secrets.env"
  chmod 600 "$work/x.secrets.env"

  # Known repo, no environment yet.
  python3 - "$FLEET_CONFIG" "$2" <<'PY'
import sys
path, url = sys.argv[1:]
text = open(path).read()
if "[repo.fixture]" not in text:
    open(path, "a").write(f'\n[repo.fixture]\nurl = "{url}"\n')
PY
  if fleet up "$target" >"$work/o" 2>&1; then fail "up before provision should fail"; fi
  grep -q "run 'fleet provision' first" "$work/o" || fail "unhelpful error"
  pass "up refuses a missing env"
  run fleet stop "$target"
  [ "$changed" = 0 ] || fail "stop on a missing env changed something"
  pass "stop on a missing env is a no-op"

  run fleet provision "$2" "$h" --secrets "$work/x.secrets.env"
  expect_unchanged fleet provision fixture "$h" --secrets "$work/x.secrets.env"
  [ "$(in_env sh -c 'stat -c %a "$HOME/.config/fleet/secrets.env"')" = 600 ] || fail "secrets not private"
  pass "secrets are mode 600"
  in_env sh -c 'printf "protocol=https\nhost=github.com\n\n" | git credential fill' | grep -q 'password=test-token' \
    || fail "git credential helper did not supply the token"
  pass "git credentials come from the secrets file"

  # A dirty checkout survives re-provisioning.
  in_env sh -c 'echo keep > agent-work.txt'
  expect_unchanged fleet provision fixture "$h"
  [ "$(in_env cat agent-work.txt)" = keep ] || fail "provision touched agent work"
  pass "provision keeps uncommitted work"

  run fleet up "$target"
  sleep 2
  [ "$(in_env sh -c 'grep -c started "$HOME/fixture-state/starts"')" = 1 ] || fail "pool did not start once"
  expect_unchanged fleet up "$target"
  [ "$(in_env sh -c 'grep -c started "$HOME/fixture-state/starts"')" = 1 ] || fail "second up started another pool"
  pass "up starts exactly one pool"
  in_env sh -c 'grep -q FIXTURE_SECRET=s3cret "$HOME/fixture-state/env"' || fail "secrets not in agent env"
  pass "agents receive the secrets"
  [[ "$(procs)" == up\ * ]] || fail "status: $(procs)"
  pass "status shows up"

  # Graceful: the worker keeps draining after stop returns.
  run fleet stop "$target"
  in_env pgrep -f "[f]ixture-worker" >/dev/null || fail "graceful stop killed the worker"
  pass "graceful stop leaves work running"
  expect_unchanged fleet stop "$target"
  [[ "$(procs)" == stopped\ * ]] || fail "status: $(procs)"
  for _ in $(seq 1 30); do in_env pgrep -f "[f]ixture-worker" >/dev/null || break; sleep 1; done
  in_env sh -c 'grep -q drained "$HOME/fixture-state/starts"' || fail "worker did not finish draining"
  pass "worker finished its drain"

  # A stop survives an instance restart, with no marker file to clean up.
  restart_env "$h"
  sleep 3
  [[ "$(procs)" == stopped\ * ]] || fail "stopped pool came back after restart: $(procs)"
  pass "stopped stays stopped after a restart"

  run fleet up "$target"
  restart_env "$h"
  sleep 5
  [[ "$(procs)" == up\ * ]] || fail "pool did not come back after restart: $(procs)"
  in_env pgrep -f "[f]ixture-worker" >/dev/null || fail "worker missing after restart"
  pass "started pool returns after a restart"

  # A process that ignores termination and that the repo's stop knows nothing about: the
  # fallback must still kill it.
  in_env bash -c 'trap "" TERM; nohup sleep 600 >/dev/null 2>&1 & nohup bash -c "trap \"\" TERM; exec -a stubborn sleep 600" >/dev/null 2>&1 &'
  sleep 1
  in_env pgrep -f "[s]tubborn" >/dev/null || in_env pgrep -x sleep >/dev/null || fail "stubborn process did not start"
  run fleet stop "$target" --now -v
  sleep 1
  grep -q 'killed them' "$work/last.log" || fail "--now did not need to escalate to KILL, so the fallback is untested"
  in_env pgrep -f "[f]ixture-worker" >/dev/null && fail "--now left the worker running"
  in_env pgrep -x sleep >/dev/null && fail "--now left a stubborn process running"
  [[ "$(procs)" == stopped\ 0 ]] || fail "--now left processes: $(procs)"
  pass "--now terminates everything, including a process ignoring TERM"
  expect_unchanged fleet stop "$target" --now

  in_env cat agent-work.txt | grep -q keep || fail "--now lost files"
  pass "--now keeps files"

  run fleet destroy "$target" --yes
  expect_unchanged fleet destroy "$target" --yes
  pass "destroy removed the env and repeats safely"
}

restart_env() {
  case $1 in
    remote) limactl shell "$hostvm" sudo incus restart --force fleet-fixture ;;
    laptop) limactl stop --force fleet-fixture >/dev/null 2>&1; limactl --tty=false start fleet-fixture >/dev/null 2>&1 ;;
  esac
}

exercise remote "$url"
limactl delete --force "$hostvm" >/dev/null 2>&1

echo "== Lima backend, this laptop as the host =="
cat >>"$FLEET_CONFIG" <<TOML

[host.laptop]
backend = "lima"
address = "local"
lima_autostart = "none"
TOML
git daemon --base-path="$work/git" --export-all --listen=127.0.0.1 --reuseaddr >/dev/null 2>&1 &
daemon_pid=$!
# Lima maps this address to the laptop's localhost, so nothing is exposed on the LAN.
url=git://192.168.5.2/fixture.git
exercise laptop "$url"
echo "ALL E2E CHECKS PASSED"
