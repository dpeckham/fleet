# Runs as root in the environment. Never resets a checkout: it may hold agents' work.
set -euo pipefail
checkout=/home/dev/code/$FLEET_REPO_NAME
as_dev() { runuser -u dev -- env HOME=/home/dev PATH=/usr/local/bin:/usr/bin:/bin "$@"; }

install -d -o dev -g dev /home/dev/code
if [ -d "$checkout/.git" ]; then
  as_dev git -C "$checkout" fetch --quiet origin
else
  # Clone beside the target and rename, so an interrupted clone cannot look finished.
  partial=$checkout.partial
  rm -rf "$partial"
  as_dev git clone --quiet "$FLEET_REPO_URL" "$partial"
  mv "$partial" "$checkout"
  echo FLEET_CHANGED
fi

# Bootstrap once per distinct command, recording success only after it finishes.
done_file=/home/dev/.config/fleet/bootstrap-done
install -d -o dev -g dev -m 0700 /home/dev/.config /home/dev/.config/fleet
if [ "$(cat "$done_file" 2>/dev/null || true)" != "$FLEET_BOOTSTRAP" ]; then
  runuser -l dev -c "cd $(printf %q "$checkout") && { set -a; [ ! -r ~/.config/fleet/secrets.env ] || . ~/.config/fleet/secrets.env; set +a; $FLEET_BOOTSTRAP; }" </dev/null
  printf '%s' "$FLEET_BOOTSTRAP" >"$done_file"
  chown dev:dev "$done_file"
  echo FLEET_CHANGED
fi

# These machines can lose power; don't let a cut undo what this run just recorded.
sync
