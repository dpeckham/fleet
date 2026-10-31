# Runs as root in the environment. Installs the secrets file only if its content differs.
set -euo pipefail
dir=/home/dev/.config/fleet
install -d -o dev -g dev -m 0700 /home/dev/.config "$dir"
new=$(mktemp "$dir/.secrets.XXXXXX")
trap 'rm -f "$new"' EXIT
printf '%s' "$FLEET_SECRETS_B64" | base64 -d >"$new"
if ! cmp -s "$new" "$dir/secrets.env"; then
  chown dev:dev "$new"
  chmod 0600 "$new"
  mv "$new" "$dir/secrets.env"
  echo FLEET_CHANGED
fi
