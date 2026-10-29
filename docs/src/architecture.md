# Architecture

## Components

| Part | Role |
|---|---|
| labtender service (`labtender serve`) | One process. Owns all state in SQLite (WAL) under `$LABTENDER_HOME`, runs one polling loop per host, ticks the ledger every 10 s, and serves the board and the HTTP API. All database access happens on its event-loop thread. |
| Probe (`probe.py`) | One standard-library file piped to the node's `python3` for each poll. |
| Parser specs | TOML files that turn a run's log tail into progress and an ETA. |
| Ledger | Claims, queue, leases, estimates and reconciliation flags. |
| CLI | Administration on the labtender service's machine; ledger commands over HTTP from anywhere. |
| MCP server (`labtender mcp`) | One per agent session, a thin client of the HTTP API. |
| Charters | Budgets, approvals and the reaper for cloud GPUs, stepping every 15 s beside the collector. |
| Yacht plugins | Create and destroy cloud machines for charters; called in worker threads. |

Reads are open to anyone who can reach the labtender service; changes need a bearer token from
`labtender token create`. Bind the labtender service to localhost, the LAN or a VPN interface only.

## State

| Table | Contents |
|---|---|
| `host` | Registry: transport, ssh target, lifecycle and expiry, interval, accepted tiers, services and their health paths, run link, labels, last seen, last error. |
| `service_health` | The latest health check of each host service: up or down, the detail, and when it last changed. |
| `watch` | Run folders to follow, per host, with their parser spec. |
| `snapshot` | The last probe document for each host. |
| `gpu_sample` | Per-GPU load, memory, power and temperature for the last 24 h. |
| `run` | Each followed run's latest interpretation and state. |
| `claim`, `flag` | The ledger and the current reconciliation flags. |
| `token` | Hashed bearer tokens with identity, kind and roles. |
| `charter`, `budget` | Charters with their offer, limits, state and cost, and per-project limits. |
| `meta` | This service's install id, the prefix of its cloud machines' names. |
| `event` | The append-only audit trail. |

## Probe protocol

The labtender service runs, for each host and at the host's interval (30 s or more):

```text
ssh -o BatchMode=yes <alias> python3 -I - <base64 JSON arguments>
```

with `src/labtender/probe.py` on stdin. SSH connections are shared (`ControlMaster`), and a host
registered with `--local` is probed without SSH. The probe prints one JSON document and exits:

| Key | Contents |
|---|---|
| `schema` | protocol version, currently 1 |
| `host` | platform (`wsl`, `jetson`, `linux`, `macos`), CPU model and load, memory |
| `gpus` | per GPU: load, memory, power, temperature, clocks, whether memory is unified, the source |
| `gpu_procs` | processes mapping `libcuda.so` or holding `/dev/dxg`, with `CUDA_VISIBLE_DEVICES`, `CUDA_DEVICE_ORDER` and any watched run logs they hold open |
| `runs` | for each watched folder: the tail (and optionally head) of its log and its small marker files |

GPU sources by platform:

| Platform | Source |
|---|---|
| NVIDIA discrete, WSL2 | `nvidia-smi --query-gpu` |
| Jetson | `nvidia-smi` where it reports, then `all-smi` if installed, then sysfs load, the `gpu-thermal` zone and one `tegrastats` line; memory is system RAM |
| Apple silicon | `all-smi` if installed (power and temperature without sudo), else `ioreg -c IOAccelerator`; memory is system RAM |
| Other Linux GPUs (AMD, Intel) | `all-smi` if installed |

[all-smi](https://github.com/lablup/all-smi) is used when it is already on a node's `PATH`;
labtender never installs it.

### Windows side of a WSL host

On a host whose probe reports `wsl`, the labtender service also runs a short PowerShell sample
(`windows_gpu.ps1`), sent as `powershell.exe -EncodedCommand` over the same transport, so
nothing is installed on Windows either. It reads the `GPU Engine` and `GPU Process Memory`
performance counters and maps each adapter to its PCI bus, so every Windows process lands on the
right GPU: the WSL VM (`vmwp`, which carries all WSL GPU work), compute programs such as Python,
and desktop applications on a GPU that drives a display. The sample takes a few seconds, so it
runs at most every two minutes and is reused in between. It needs English performance-counter
names. Turn it off for a host with `labtender host set <host> --label windows_gpu=off`.

Under WSL2, `nvidia-smi` lists no processes, so a process is placed on a GPU from its
`CUDA_VISIBLE_DEVICES`: by UUID, or by index when `CUDA_DEVICE_ORDER=PCI_BUS_ID` (the index then
matches `nvidia-smi`). On a single-GPU node every GPU process is on that GPU. Anything else is
shown as unpinned and counts against every GPU.

## Run parsers

A watch (`labtender watch add <host> <parser> <glob>`) names run folders on a host and a parser
spec. Specs are TOML; built-in ones live in `src/labtender/parsers/`, and a lab's own go in
`$LABTENDER_HOME/parsers/`. A spec gives:

- `log`: the progress file in each folder, and `markers`: small files to read whole;
- `[progress]`: a regex with named groups `t`, `t_unit`, `rate` and `iter`, or JSON-lines fields;
- `[[target]]`: where the simulated end time comes from (a marker, the folder name, the head or
  tail of the log), tried in order;
- `[done]`: an exit-code marker, a final log line, or reaching the target;
- `[card]`: an optional marker naming the GPU.

The labtender service turns the last progress line into percent complete, wall rate and an estimated finish.
Node clock skew does not matter: ages are measured against the probe's own clock. A run is tied
to its GPU through the GPU process holding its log open; such a run counts as running however
long its log is quiet. Otherwise a run whose log falls silent for `stale_after_min` is shown as
stale, and one whose folder disappears is marked gone. See [](runs.md) for writing specs.

## Metrics

`GET /metrics` serves the labtender service's view in the Prometheus text format, so a lab can scrape it and
build Grafana dashboards: `labtender_host_up`, `labtender_host_last_seen_seconds`, per GPU
`labtender_gpu_utilization_percent`, `labtender_gpu_memory_used_bytes`,
`labtender_gpu_memory_total_bytes`, `labtender_gpu_power_watts`,
`labtender_gpu_temperature_celsius`, `labtender_gpu_processes`, `labtender_gpu_claimed`,
`labtender_gpu_queue_length` and `labtender_gpu_flag{kind=...}`, per checked host service
`labtender_service_up{host=...,service=...}` (1 up, 0 down), per running run
`labtender_run_progress_ratio` and `labtender_run_eta_seconds`, and for charters
`labtender_charter_active{state=...}`, `labtender_charter_cost_dollars`,
`labtender_charter_adrift` (alert on any), `labtender_budget_reserved_dollars{window=...}` and
`labtender_budget_limit_dollars{window=...}`.

## Claim ledger

The ledger's rules, estimates, flags and `preflight` are described in [](ledger.md); the agent
interface in [](agents.md).

## Charters

Budgets, approvals and the reaper are described in [](yachts.md).

## Backups

The labtender service copies its database once a day with SQLite's online backup, safe while it
runs, into `$LABTENDER_HOME/backups/`, keeping the newest seven. `labtender backup [--to FILE]`
makes one on demand. To restore, stop the service and copy a backup over `labtender.sqlite`.

## Audit

Host and watch changes, token issue and revocation, every run state change, every claim
transition and every reconciliation flag are written to an append-only `event` table; triggers
reject updates and deletes.
