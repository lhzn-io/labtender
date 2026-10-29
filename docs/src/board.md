# Reading the board

The board at `/` has one panel per host; `/compact` packs the same information into a narrow
column. Both refresh every 15 s.

```{figure} _static/board.png
:alt: The labtender board for a fictional lab
:width: 100%
```

## Host header

A green dot means the host answered within three poll intervals; red means it did not, and the
last error appears under the header. The line below gives the platform (`wsl`, `jetson`,
`linux`, `macos`), CPU model, CPU load, RAM, network traffic in and out (Linux hosts, measured
over the probe's half-second window), free disk in the probing user's home, the probe's own run
time, and, for ephemeral hosts,
the time left before expiry. A host with services lists them next, each linked, with *up* or
*down since* and the reason for those that have a health check (see below).

## GPUs

Each GPU shows:

| Item | Meaning |
|---|---|
| load | GPU utilisation from the platform's own source (see [](architecture.md)). |
| memory | Used over total. On Jetson and Apple silicon the GPU shares system RAM, so memory is labelled *unified memory* and shows the system's figure. |
| sparkline | GPU load over the last hour. |
| facts | Temperature, power and limit, clock, *drives a display* if the GPU has a monitor attached, and the data source. |
| processes | Processes placed on this GPU, with run time and resident memory. |
| Windows | On a WSL host: the Windows processes on this GPU, with the WSL VM and compute programs by name and desktop applications summed, from the Windows performance counters. |
| ledger box | The holder and estimated return, the queue with expected start times, and any flags (see below). |

Processes that use the GPUs but are not pinned to one of them are listed under the host, with the
reason ("not pinned to one GPU; counts on every GPU").

## Runs

Running runs come first, each with a progress bar, simulated time over target, wall rate, the
estimated finish as a clock time, the GPU, and how long ago its log last moved. Finished,
exited, stale and vanished runs fold into *earlier runs*.

A job that two watches both follow, such as a wrapper's folder and the job's own log folder,
shows once while one live process holds both: the run whose log moved last, with the other
folders in its `also` field in `GET /api/state`.

| State | Meaning |
|---|---|
| running | The log moved recently, or a live GPU process holds it open. |
| stale | The log has been silent for longer than the spec's `stale_after_min` (default 30 min) and no live process holds it. Often a run that stopped early. |
| done | The exit marker says 0, the log ends with the done line, or the run reached its target time. |
| exited | The exit marker says something other than 0. |
| gone | The run's folder disappeared from the node while it was running or stale. |

## Run links

A host can carry named service addresses and a run-link template, so each run's name on the board
links to another tool, such as a results dashboard running on that host:

```bash
labtender host set gpu-box --service dashboard=http://gpu-box.lan:9000 \
    --run-link "{service:dashboard}/run/{name}"
```

The template may use `{service:NAME}`, `{name}` (the run folder's name), `{dir}` (its full path)
and `{host}`. A run whose template names a service the host does not offer gets no link. Other
tools can find hosts by service with `GET /api/hosts?service=dashboard`.

## Service health checks

A service can carry a health path. The labtender service then requests the service's address
plus that path once per poll of its host, from its own machine, with a 5 s timeout:

```bash
labtender host set gpu-box --service dashboard=http://gpu-box.lan:9000 --health dashboard=/healthz
labtender host set gpu-box --health dashboard=        # stop checking it
```

Any 2xx or 3xx answer counts as up; redirects are not followed. An error status, a refused
connection, or no answer within the timeout counts as down. A service that still listens but has
stopped answering, for example with its accept backlog full, shows as *no answer in 5 s*. The
checks run in their own task, so a hung service never delays a probe, and only one round of checks
per host runs at a time.

### Fallback addresses

A service can also carry fallback addresses, for a host that is reached a different way when it
is away, such as a laptop on a VPN:

```bash
labtender host set ws-laptop --service dashboard=http://ws-laptop.lan:9000 --health dashboard=/healthz \
    --fallback dashboard=http://ws-laptop-vpn.lan:9000
labtender host set ws-laptop --fallback dashboard=     # remove them
```

Each check tries the service's own address first, then each fallback in order, and the first that
answers is where the service is now: its *active address*. Run links and the service's link on the
board use the active address, so they keep working while the host is away; the board shows
*up via* the fallback's host name. When no address answers, the active address is the service's
own and the reason says how many fallbacks were tried. A move from one address to another while
the service stays up is audited as `service.moved`.

Fallbacks only take effect through health checks: a service without a health path always uses
its own address. Results appear on the board, in `GET /api/hosts`, in the MCP
`fleet_status` tool and as `labtender_service_up` in `/metrics`, and each change between up and
down is written to the audit trail.

## Flags

Flags are information for people, never instructions. They are raised and cleared after each
probe and whenever a claim changes, and every change is written to the audit trail.

| Flag | Raised when |
|---|---|
| unclaimed use | A GPU has processes running on it, or load of 50 % or more, and nobody holds a claim on it. |
| idle claim | A claim has been held for more than 20 min on a GPU with no processes and load under 5 %. |
| unpinned | On a host with more than one GPU, a GPU process is not pinned to a single GPU. |
| GPU missing | A GPU the host reported within the last day is absent from its latest probe, for example an external GPU unplugged for travel. Its claims pause until it returns. A card that has turned up on another registered host has moved instead: no flag, and its old claims end. |
| service down | A host service with a health path failed its latest check: an error status, a refused connection or no answer within 5 s. Host-level; it lists each service and why, and clears when they answer again. |

## Prices

The board's header links to `/prices`: current GPU prices per provider, their trend, and the run
rate of live yachts. See [](yachts.md).

## Gauges

```{figure} _static/gauges.png
:alt: The gauges page: one tile per GPU with round gauges for load, memory, power and temperature
:width: 100%

The gauges page for the same fictional lab.
```

The header links to `/gauges`: every GPU on every host on one page, for a wall screen or a
second monitor. Each card is a tile named by its card name, with round gauges for load (and core
clock), memory, power against its limit and temperature, a chart of load and memory over the last
hour (hover for the readings at a moment), who holds it and how many wait, and how fresh the
reading is. Memory and power turn amber near their ceiling and red at it, and temperature at 80 C
and 90 C, with the label saying so. A GPU missing from its host's latest probe shows as away.
Totals across the lab sit on top: GPUs reporting, busy and held, power drawn, memory in use and the
hottest card. The page refreshes every 15 s; readings are as fresh as each host's probe interval
(45 s by default, `labtender host set NAME --interval`).

## The compact view

```{figure} _static/board-compact.png
:alt: The compact view, one line per GPU with holders, queues and running jobs
:width: 420px

The compact view of the same fictional lab.
```

Each host is a block: a dot, the name, and a warning on the right (unreachable, stale runs, a
service down, unpinned GPU use). Each GPU is one line with load and memory bars and figures, followed by its
holder and estimated return, the number waiting, and any flags. Running runs follow with their
GPU, name, progress and finish time. The header links to the full board.
