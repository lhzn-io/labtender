# Contributing to labtender

Thank you for helping. A few rules keep the project small and safe to run on shared machines.

## Ground rules

- **Do no harm to the fleet.** Collection is read-only. The probe never starts, stops or pins
  work, never needs root, and keeps to one short run per poll (30 s or more apart).
- **Keep the probe portable.** `src/labtender/probe.py` is piped to the node's own `python3`, so
  it must stay one file, standard library only, and run on Python 3.8.
- **Claims are advisory.** labtender records and reconciles; it does not kill or pre-empt jobs.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```

Test fixtures are synthetic. Do not commit real hostnames, users, paths or log lines from your
own lab.

## Commits

Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`), written in plain prose with no
emojis. Each commit is authored by the person responsible for it; tools that helped may be named
in the body as plain text, not as `Co-Authored-By` trailers.

## Licence

By contributing you agree that your contributions are licensed under the Apache License 2.0.
