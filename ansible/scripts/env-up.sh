# Runs as root in the environment. The service manager holds the started/stopped state.
# Counts dev's processes, leaving out the session manager that any login leaves behind.
agent_count() { ps -u dev -o comm= 2>/dev/null | grep -cvE '^(systemd|\(sd-pam\))$' || true; }
set -euo pipefail
unit=/etc/systemd/system/fleet-agents.service
start=/usr/local/libexec/fleet-start
install -d /usr/local/libexec

new_start=$(mktemp)
new_unit=$(mktemp)
trap 'rm -f "$new_start" "$new_unit"' EXIT
cat >"$new_start" <<START
#!/bin/bash -l
cd /home/dev/code/$FLEET_REPO_NAME
exec bash -c $(printf %q "$FLEET_START")
START
# KillMode=process: stopping the unit must leave a draining pool running to completion.
cat >"$new_unit" <<'UNIT'
[Unit]
Description=fleet agent pool
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=dev
EnvironmentFile=-/home/dev/.config/fleet/secrets.env
ExecStart=/usr/local/libexec/fleet-start
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT

reload=0
if ! cmp -s "$new_start" "$start"; then
  install -m 0755 "$new_start" "$start"
  reload=1
fi
if ! cmp -s "$new_unit" "$unit"; then
  install -m 0644 "$new_unit" "$unit"
  reload=1
fi
if [ "$reload" = 1 ]; then
  systemctl daemon-reload
  echo FLEET_CHANGED
fi

if ! systemctl is-enabled --quiet fleet-agents; then
  systemctl enable --quiet fleet-agents
  echo FLEET_CHANGED
fi
if ! systemctl is-active --quiet fleet-agents; then
  systemctl start fleet-agents
  echo FLEET_CHANGED
elif [ "$(agent_count)" = 0 ]; then
  # The unit says started but the pool has died; starting is safe to repeat.
  systemctl restart fleet-agents
  echo FLEET_CHANGED
fi

# The started/stopped choice must survive a power cut.
sync
