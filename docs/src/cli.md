# Command line

`labtender` runs the labtender service and talks to it. Commands fall into two groups:

- **Service administration** (`serve`, `host`, `watch`, `parsers`, `probe`, `status`, `token`,
  `events`) work on the labtender service's SQLite file directly, so run them on the labtender service's machine.
- **Ledger commands** (`claim`, `claims`, `heartbeat`, `bind`, `release`, `whoami`,
  `preflight`) and `mcp` go through the HTTP API as the identity of `LABTENDER_TOKEN`, from any
  machine that can reach the labtender service.

## Environment

| Variable | Used by | Default |
|---|---|---|
| `LABTENDER_HOME` | labtender service administration | `~/.config/labtender` |
| `LABTENDER_URL` | ledger commands, `preflight`, `mcp` | `http://127.0.0.1:8795` |
| `LABTENDER_TOKEN` | ledger commands, `preflight`, `mcp` | none (reads still work) |
| `LABTENDER_HOST` | `preflight`: this host's registry name | the hostname |
| `LABTENDER_ACTOR` | labtender service administration: the name in the audit trail | the login name |

## Commands

The help text below is generated from the program itself when the documentation is built.

```{include} _generated/cli-help.md
```
