# fleet

Provisions environments on your machines and runs agent pools in them. It is glue around
Tailscale, Ansible, Incus and Lima. Run it from your laptop; state is read live from the hosts.

## Setup

```sh
mise trust && mise run setup
ln -s "$PWD/bin/fleet" ~/.local/bin/fleet     # or use it from this directory via mise

eval "$(fleet completion bash)"               # in ~/.bashrc (needs bash 4.4+, not macOS's /bin/bash)
eval "$(fleet completion zsh)"                # in ~/.zshrc, after compinit
```

`fleet completion bash` removes `@` from `COMP_WORDBREAKS` so `REPO@HOST` completes as one word.

## Use

`fleet.toml` holds your hosts and repos. It is git-ignored because it names your machines, so back it
up privately or point `FLEET_CONFIG` at a private location.

```sh
fleet host add box1 --ssh me@192.0.2.10 --backend incus
fleet provision me/app box1 --secrets app.secrets.env
fleet attach app@box1        # first-time logins, e.g. Claude Code
fleet up app@box1
fleet status
fleet stop app@box1          # or --now
```

Every command and subcommand has `--help` with usage and examples. Every command except
`destroy` can be repeated safely. Stopped/started state is the systemd unit's enabled state in the
environment, so there are no marker files to clean up.

## Environments

No ISO or image file is needed. Each repo gets one generic Debian 13 environment (`--image` or
`default_image` to change it), with a `dev` user, mise, and the repo at `~/code/REPO`. The repo's
`mise.toml` supplies its tools. Debian-family images are required, since provisioning uses apt and
systemd.

A repo's commands default to `mise trust && mise install`, then `agents --conf=config/agents.conf`
and `agents stop [--now]`. Override `bootstrap`, `start`, `stop` and `stop_now` under `[repo.NAME]`
in `fleet.toml`. See `fleet.toml.example`.

## Tailscale

`host add` needs `TAILSCALE_API_KEY`, and `tag:fleet-host` declared in your tailnet policy's
`tagOwners`. fleet does not edit your policy. Add grants so your laptop reaches hosts on port 22 and
hosts can't reach your laptop or each other.

## Tests

`mise run test` covers the CLI, every `--help`, and real Tab presses in bash and zsh.
`mise run e2e` creates and deletes throwaway Lima VMs; it needs no real hosts.

## Not built yet

macOS host-user and Tart environments, Windows VMs, `fleet upgrade`, and sharing claims across hosts
(run each pipeline on one host per repo).
