# labtender: context for AI coding agents

## Layout

- `src/labtender/probe.py`: the node-side probe. One file, standard library only, Python 3.8+,
  piped to `python3 -I -` over SSH with its arguments as base64 JSON in `argv[1]`. It prints one
  JSON document (`schema` 1) and exits. Never add dependencies or write files here.
- `src/labtender/collector.py`: per-host polling loops, SSH connection sharing, backoff, and
  placing GPU processes on GPUs from `CUDA_VISIBLE_DEVICES` (`attribute`).
- `src/labtender/parsers/`: run-log parser specs (TOML) and the interpreter that turns a log tail
  into progress, state and an ETA. Lab-specific specs belong in `$LABTENDER_HOME/parsers/`, not here.
- `src/labtender/db.py`: SQLite (WAL) store. The `event` table is an append-only audit trail,
  enforced by triggers.
- `src/labtender/ledger.py`: claims, queue, leases, estimates and reconciliation flags. All state
  changes go through `Store.tx()` and are audited. Takes an injectable clock for tests.
- `src/labtender/client.py`: standard-library HTTP client for the labtender service (`LABTENDER_URL`,
  `LABTENDER_TOKEN`); used by the claim commands and preflight.
- `src/labtender/preflight.py`: local occupancy and memory check plus the ledger, for run wrappers.
- `src/labtender/mcp_server.py`: stdio MCP server (`labtender mcp`), a thin client of the HTTP
  API. Its instructions to agents are `src/labtender/prompts/mcp_instructions.txt`, read at
  runtime; edit the file, never inline prompt text in code.
- `src/labtender/server.py`: FastAPI labtender service. Handlers are `async` so all database access stays on the
  event-loop thread. Reads are open; changes need a bearer token.
- `src/labtender/board/index.html`: the board, plain HTML and JS with no build step.
- `src/labtender/cli.py`: `labtender` command.
- `src/labtender/backup.py`: daily online backups of the state database.
- `src/labtender/charter.py`: cloud GPU charters (yachts): worst-case pricing, budgets and
  pre-approval limits, purser approval, the lifecycle task and the reaper. Plugin calls run in
  worker threads; all database access stays on the event loop.
- `src/labtender/yachts/`: the plugin interface (`quote`, `create`, `destroy`, ...) and the
  built-in plugins. `fake` needs no cloud and backs the tests and the demo. `skypilot.py` runs
  `sky_helper.py` under SkyPilot's own Python (a separate uv tool), one JSON request per call;
  `sky_helper.py` must run on Python 3.9+ and import nothing from labtender.

Money rule: nothing that can spend money may bypass `Charters.request`/`approve` (the budget
checks) or skip setting a provider-side time limit at create. Tests use the `fake` plugin;
never point a test or a demo at a real cloud.
- `contrib/`: the Windows launcher, and systemd and launchd units for running the service.

Terminology: the long-running process is "the labtender service" (started by `labtender serve`).
Avoid "hub" and "agent" for it: "agent" means an AI agent session, and other tools in a lab may
have their own hubs.

## Commands

```bash
uv pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
labtender serve --no-collect     # board without polling
uv pip install -e ".[docs]"
sphinx-build -W -b html docs/src docs/build/html
sphinx-autobuild docs/src docs/build/html --port 8796   # live preview
```

Documentation lives in `docs/src/` (Sphinx with MyST Markdown, Furo theme). Draw diagrams as
```` ```mermaid ```` fences (sphinxcontrib-mermaid), not ASCII art. The CLI reference is
generated from `build_parser()` at build time; when you change behaviour, update the matching
page (`ledger.md`, `runs.md`, `agents.md`, `http-api.md`) in the same commit.

State lives in `$LABTENDER_HOME` (default `~/.config/labtender/`).

## Rules

- Collection is read-only on fleet nodes: never start, stop or pin jobs while testing, and keep
  poll intervals at 30 s or more.
- Fixtures and docs use synthetic hosts, users, paths and log lines only.
- No emojis or em-dashes in code, docs or commits. Conventional commits.
