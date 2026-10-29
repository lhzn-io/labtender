# Getting started

This page takes you from nothing to a board that shows your machines and their runs, then to
your first claim.

## What you need

- **One labtender service machine** with Python 3.11 or later. It needs SSH access to every node. A WSL2
  distribution, a Linux server or a Mac all work; pick one that stays up.
- **Nodes** reachable with key-based SSH (`ssh <node>` works without a password prompt) and a
  `python3` of 3.8 or later. Nothing is installed on them.
- On NVIDIA nodes, `nvidia-smi`. On Jetson, `tegrastats` helps but is optional. On macOS nothing
  extra is needed (`ioreg` ships with the system, and no sudo is used).

## Install the labtender service

From a checkout of the repository:

```bash
uv venv && uv pip install -e ".[dev]"
```

or install the command for your user, so `labtender` is on your `PATH` and follows the checkout:

```bash
uv tool install -e .
```

The labtender service keeps its state in `$LABTENDER_HOME` (default `~/.config/labtender/`): the SQLite
database, SSH connection sockets, and any parser specs of your own.

## Register hosts

Each host is named after an alias in your `~/.ssh/config`, so user names, ports, jump hosts and
keys stay where they already are:

```bash
labtender host add gpu-box             # ssh gpu-box
labtender host add jetson-1 --ssh ubuntu@jetson-1.lan
labtender host add mac-studio
labtender host add workstation --local # the labtender service's own machine, probed without ssh
```

A host is polled every 45 s by default (30 s at the least); change it later with
`labtender host set`, which also sets labels, services and run links. A host
that should only take low-priority work can say so with `--accepts nice-to-have` (see
[](ledger.md)). Short-lived hosts take `--expires-in HOURS` and leave the registry on their own.

Check one host before starting the labtender service:

```bash
labtender probe gpu-box
```

```text
gpu-box: linux, AMD EPYC 7543, CPU 12 %, seen 0 s ago
  GPU 0 NVIDIA RTX 6000 Ada: 97.0 %, 31.2/48 GiB, 280.4 W, 71.0 C
    pid 4121 python (2 h 14 min)
  GPU 1 NVIDIA RTX 6000 Ada: 0.0 %, 0.4/48 GiB, 22.1 W, 38.0 C
```

## Follow your runs

labtender reads the progress of long runs from their own log files. Tell it where the run
folders are and which parser spec reads them:

```bash
labtender parsers                       # list the specs it knows
labtender watch add gpu-box run-logdir '~/jobs/logs/*/'
```

The board then shows each run's progress, wall rate and estimated finish. If your runs write a
different format, write a spec of your own; see [](runs.md).

## Start the labtender service

```bash
labtender serve                         # http://127.0.0.1:8795/
```

Open `http://127.0.0.1:8795/` for the board, or `/compact` for a narrow view to keep in a
corner of the screen. `labtender status` prints the same picture as text.

The labtender service binds to `127.0.0.1` by default. To let other machines reach it, bind it to a LAN or VPN
address with `--bind`; never expose it to the internet (see [](safety.md)).

:::{tip}
On Windows with the labtender service in WSL, `contrib/windows/start-labtender.cmd` starts the labtender service in its own
minimised console if it is not answering, then opens `/compact` as a small Edge app window.
Closing the window leaves the labtender service running. With `LABTENDER_URL` set (in the shell,
or saved with `setx`, which shells opened earlier do not see), it only opens the window on that
service. It never starts a local service that has no database yet, since that would show an empty
board.
:::

## Make your first claim

Changes to the ledger need a token. Create one for yourself and one for each agent session:

```bash
labtender token create alice --kind person > ~/.config/labtender/token-alice
export LABTENDER_TOKEN=$(cat ~/.config/labtender/token-alice)
labtender whoami
```

A person's token can claim GPUs and change the registry; give agents and view-only users
narrower roles with `--role` (see [](ledger.md)). Then claim a GPU, see the queue, and release it
when you are done:

```bash
labtender claim gpu-box/gpu1 --hours 3 --note "fine-tune, 8 GiB"
labtender claims gpu-box/gpu1
labtender release 1
```

Agent sessions do the same through MCP; see [](agents.md).
