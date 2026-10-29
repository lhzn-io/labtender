# Running the service

`labtender serve` is one process. Run it where it can stay up: a machine that is always on and
on the lab network, rather than a laptop that sleeps or travels. Every other machine, including
laptops, is then just a host the service probes, and people and agents reach the service over
the LAN or a VPN.

## Choosing where it runs

| Machine | Fit |
|---|---|
| An always-on desktop, Mac or small server on the lab network | Best: it never sleeps and every node can reach it. |
| A GPU workstation | Fine if it stays on; the service itself is light. |
| A laptop that travels | Avoid: while it sleeps or roams, nobody can claim GPUs. Probe it as a host instead. |

Bind the service to the addresses people use, never to the internet:

```bash
labtender serve --bind 192.0.2.10 --port 8795      # the machine's LAN or VPN address
```

## Linux and WSL (systemd user service)

`contrib/systemd/labtender.service` runs the service as your user and restarts it if it stops:

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/labtender.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now labtender
journalctl --user -u labtender -f          # logs
sudo loginctl enable-linger "$USER"        # keep it running without a login session
```

On WSL, systemd must be enabled (`[boot] systemd=true` in `/etc/wsl.conf`), and the distribution
only runs while Windows has started it; a scheduled task that runs `wsl.exe -e true` at logon
brings it up after a reboot.

## macOS (launchd agent)

`contrib/launchd/io.lhzn.labtender.plist` runs the service at login and restarts it if it stops.
Edit the paths in it to where `labtender` is installed, then:

```bash
cp contrib/launchd/io.lhzn.labtender.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.lhzn.labtender.plist
launchctl print gui/$(id -u)/io.lhzn.labtender | head      # status
```

Logs go to `~/Library/Logs/labtender.log`.

## Moving the service

The state is one SQLite file. To move the service to another machine:

1. `labtender backup --to labtender.sqlite` on the old machine, then stop the service there.
2. Copy the file to `$LABTENDER_HOME/labtender.sqlite` on the new machine, along with any parser
   specs in `$LABTENDER_HOME/parsers/`.
3. Make sure the new machine can `ssh` to every host (the same `~/.ssh/config` aliases), then
   start the service there.
4. Point clients at it: `LABTENDER_URL=http://<new-machine>:8795` for the CLI, `preflight` and
   `labtender mcp`. Tokens move with the database.

Hosts registered with `--local` refer to the old machine. On the new machine, probe the old one
over SSH and the new one locally:

```bash
labtender host set old-machine --transport ssh --ssh old-machine
labtender host set new-machine --transport local
```

Registry and token commands write the database directly, so run them on the new machine from now
on. On Windows, set `LABTENDER_URL` (for example with `setx`) so `start-labtender.cmd` opens the
remote service instead of starting a local one.

## Laptops that travel

A WSL host probed over SSH needs an SSH server inside WSL that the service's machine can reach
(with WSL's mirrored networking, WSL shares the laptop's address). The Windows GPU sample then
runs `powershell.exe` through WSL interop, from the Windows `PATH` or its standard location.

A laptop probed as a host can leave and come back. While it is unreachable its claims pause, and
when it returns (for example over a VPN from elsewhere) probing resumes on its own. If the laptop
carries an external GPU that stays behind, that GPU shows as missing and its claims and queue wait
for it.
