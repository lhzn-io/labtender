# Following runs

Long jobs already write progress to their own logs. labtender reads those logs, so the board can
show progress and an estimated finish without any change to the job.

## Watches

A watch names run folders on a host and the parser spec that reads them:

```bash
labtender watch add gpu-box run-logdir '~/jobs/logs/*/'
labtender watch ls
labtender watch rm 3
```

On each poll the probe expands the glob on the node, keeps the newest folders (eight by default)
touched within the spec's `max_age_h` (72 h by default), and returns the tail of each folder's
log, optionally its head, and its small marker files. Parsing happens on the labtender service.

A run is tied to its GPU through the GPU process that holds its log open. That link also keeps
a run *running* through long quiet stretches, and lets a claim bound to the run stay alive (see
[](ledger.md)).

Screenshots in these docs come from `scripts/demo_state.py`, which seeds a fictional lab into
an empty `$LABTENDER_HOME` for a labtender service started with `--no-collect`.

## Built-in specs

`run-wrapper`
: A folder per run made by a launcher script: `stdout.log` with a progress line now and then,
  an `args` file with the launch arguments (the target from `--hours 48`), a `device` file with
  the CUDA pin, and a `status` file holding the exit status, written at the end. Progress lines
  look like `step 30000 | time 36.0 h | speed 10.0 h/h`.

`run-logdir`
: A job's own log folder: `run.log` with its planned length near the top (`duration_h = 48`)
  and progress lines below, like `[12:28:14] step=8000 time=11.1h speed=1.7h/h`. The job is
  done when it writes a `DONE` file or reaches its planned length; a `duration_h` file in the
  folder overrides the length in the log, for a job extended after it started.

They are examples as much as defaults: copy one into `$LABTENDER_HOME/parsers/` under another
name and change the file names and the progress regex to match your own jobs.

## Writing a spec

Put a TOML file in `$LABTENDER_HOME/parsers/`. A spec with the same `name` as a built-in one
replaces it. An annotated example:

```toml
name = "my-trainer"
description = "One folder per training run with a JSON-lines progress file"
log = "progress.jsonl"          # the progress file inside each run folder
markers = ["config.txt"]        # small files read whole (up to 4 KiB)
head_bytes = 0                  # also read the start of the log (for targets set there)
tail_bytes = 65536              # how much of the end of the log to read
stale_after_min = 30            # silent this long, and no process holds it: stale
max_age_h = 72                  # ignore folders untouched for longer
max_dirs = 8                    # newest folders per watch

[progress]
format = "jsonl"                # or "regex" (the default)
t_field = "epoch"               # simulated (or logical) time
t_unit = "h"                    # unit of t: s, min, h, d (or a t_unit_field)
rate_field = "epochs_per_hour"
rate = "sim_h_per_wall_h"       # or "wall_min_per_sim_h"
iter_field = "step"

[[target]]                      # where the end time comes from; tried in order
marker = "config.txt"
regex = 'epochs=(?P<value>\d+)'
unit = "h"

[done]
marker = "status"               # file holding the exit status
line = "DONE"                   # or a final line in the log
file = "finished"               # or a file whose presence alone means it finished
at_target = true                # done once t reaches the target
near_target_pct = 99.0          # a silent run this close to its target counts as done
```

For text logs, use a regex with named groups instead:

```toml
[progress]
regex = 'epoch (?P<iter>\d+) at (?P<t>[\d.]+) (?P<t_unit>h|d), (?P<rate>[\d.]+) min per h'
rate = "wall_min_per_sim_h"
```

Targets can come from a marker file (`marker` and `regex`), the folder name (`dirname`), the
start of the log (`head`, with `head_bytes` set), or the log tail (`log`). Each needs a named
group `value` and a `unit`. A `[card]` table with `marker` and a regex group `card` reads the
CUDA pin from a file, for runs whose process cannot be inspected.

## How the estimate is made

From the last progress line, the labtender service takes the simulated time `t`, the target `T` and the wall
rate `r` (wall minutes per simulated hour):

- progress is `t / T`;
- the time remaining is `(T - t) * r` minutes;
- the estimated finish is the time the log last moved plus that remainder.

Ages are measured against the node's own clock and then re-based on the labtender service's, so clock skew
between machines does not shift estimates.
