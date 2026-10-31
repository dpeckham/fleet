# Runs as root in the environment. Disabling the unit is what keeps agents stopped across reboots.
# Counts dev's processes, leaving out the session manager that any login leaves behind.
agent_count() { ps -u dev -o comm= 2>/dev/null | grep -cvE '^(systemd|\(sd-pam\))$' || true; }
set -euo pipefail
checkout=/home/dev/code/$FLEET_REPO_NAME
repo_command() {
  [ -d "$checkout" ] || return 0
  # A hung repo command must not block the fallback below.
  timeout 60 runuser -l dev -c "cd $(printf %q "$checkout") && { set -a; [ ! -r ~/.config/fleet/secrets.env ] || . ~/.config/fleet/secrets.env; set +a; $1; }" </dev/null \
    || echo "fleet: repo stop command failed or timed out" >&2
}

was_up=0
if systemctl is-enabled --quiet fleet-agents 2>/dev/null || systemctl is-active --quiet fleet-agents 2>/dev/null; then
  was_up=1
fi

if [ "$FLEET_NOW" = 1 ]; then
  if [ "$(agent_count)" != 0 ]; then
    repo_command "$FLEET_STOP_NOW"
    if [ "$(agent_count)" != 0 ]; then
      pkill -TERM -u dev || true
      for _ in 1 2 3 4 5; do
        [ "$(agent_count)" != 0 ] || break
        sleep 1
      done
      if [ "$(agent_count)" != 0 ]; then
        pkill -KILL -u dev || true
        echo "fleet: repo stop left processes running; killed them" >&2
      fi
    fi
    echo FLEET_CHANGED
  fi
elif [ "$was_up" = 1 ]; then
  repo_command "$FLEET_STOP"
fi

if [ "$was_up" = 1 ]; then
  systemctl disable --quiet fleet-agents 2>/dev/null || true
  systemctl stop fleet-agents 2>/dev/null || true
  echo FLEET_CHANGED
fi

# The started/stopped choice must survive a power cut.
sync
