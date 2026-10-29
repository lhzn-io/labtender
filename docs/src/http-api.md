# HTTP API

The labtender service serves JSON under `/api/`. Reads are open to anyone who can reach the labtender service. Changes need
`Authorization: Bearer <token>`; the token's identity becomes the owner or actor, and its roles
decide what it may change:

| Role | May |
|---|---|
| `viewer` | Read (and identify itself through `/api/whoami`). Reads need no token today; the role lets other services check a token for read access. |
| `operator` | Claim, heartbeat, bind and release. |
| `admin` | Add, change and remove hosts. |
| `purser` | Approve and reject charters, and set budgets. People only. |

A person's token gets `viewer`, `operator` and `admin` by default and an agent's `viewer` and
`operator`; choose others with `labtender token create --role`. `purser` is never a default:
grant it with `labtender token grant <identity> purser`. Behind an authenticating proxy, a
signed-in person needs no token; see [](sign-in.md). The labtender service also serves interactive OpenAPI
documentation at `/docs`.

Errors return a JSON body with a `detail` message: 401 for a missing or unknown token, 403 for a
missing role or a forced release by an agent, 404 for an unknown host, 409 for a ledger refusal
(for example a claim owned by someone else), and 422 for invalid input.

## Pages

| Method and path | Returns |
|---|---|
| `GET /` | The board. |
| `GET /compact` | The compact view. |
| `GET /healthz` | `{"ok": true, "time": ...}`. |
| `GET /metrics` | Prometheus text format (see [](architecture.md)). |

## Fleet

| Method and path | Returns |
|---|---|
| `GET /api/state` | Every host with its last probe: platform, CPU, memory, GPUs (with processes and a `ledger` view each), unplaced processes, runs from the last 72 h, `service_health`, and host-level flags in `host_flags` (such as `service-down`). |
| `GET /api/hosts?service=` | The registry, each host with its `service_health` and `services_active` (each service's address to use now, which may be a fallback); with `service`, only hosts that offer it. |
| `POST /api/hosts` | Register a host (admin). Body: `name`, `transport` (`ssh` or `local`), `ssh`, `lifecycle`, `expires_in_h`, `interval_s`, `accepts`, `labels`, `services`, `health`, `fallbacks`, `run_link`. |
| `PATCH /api/hosts/{name}` | Change a host (admin). Body: any of `interval_s`, `accepts`, `ssh`, `services` (merged; an empty address removes a service and its health path), `health` (merged; `{service: path}`, an empty path stops checking), `fallbacks` (`{service: [address, ...]}`, each list replacing that service's, an empty list removing it), `run_link` (empty clears it). Labels are set with `labtender host set --label`. |
| `DELETE /api/hosts/{name}` | Remove a host (admin). |
| `GET /api/runs?host=&dir=` | Runs from the last 72 h; `dir` picks one run folder exactly (with or without a trailing slash). Each run carries a `link` when its host has a run-link template. |
| `GET /api/history/{host}/{card}?minutes=60` | GPU samples (load, memory, power, temperature), up to 24 h. |
| `GET /api/gauges?minutes=60` | Every GPU on every host in one answer, for the gauges page, grouped by `kind` (`discrete`, `integrated`, `apple`; listed in `groups`) and ranked by `bandwidth_gbs` within a group: `host`, `index`, `card_name`, `hold`, `model`, `online`, latest load, memory, power and limit, temperature and clock, `procs`, `holders`, `queued`, and `history` (samples over the window, up to 24 h). A GPU missing from its host's latest probe is listed with `away: true`. |
| `GET /api/events?limit=100` | The audit trail, newest first. |
| `GET /api/cards` | Every card: `uuid` (`local:host/gpuN` for a GPU with none), `name`, `model`, where it was last seen (`host`, `idx`, `last_seen`), `hold` (why it is held back from agents, or null) and `bandwidth_gbs` (a figure a person gave, or null for the published one). |
| `PUT /api/cards/{card}` | Change a card (admin), found by name, `host/gpuN` or UUID. Body fields, each optional: `name` (empty gives back the name made from its UUID; the name follows the card between hosts), `hold` (a reason holds it back from agents, null offers it again) and `bandwidth_gbs` (null for the published figure). |

## Ledger

| Method and path | Returns |
|---|---|
| `GET /api/whoami` | The token's `identity`, `kind` and `roles` (any token). 401 for an unknown or revoked one. |
| `GET /api/resources/{resource}` | Holders with estimated end, queue with expected start, `free_at`, flags. |
| `GET /api/claims?resource=&owner=&all=false` | Active claims, or all with `all=true`. |
| `POST /api/claims` | Claim or queue (operator). Body: `resource`, `tier`, `est_hours`, `note`, `share`, `lease_s`, `pickup_min`. |
| `POST /api/claims/{id}/heartbeat` | Keep alive, take up an offer, revise `est_hours`, or change the lease with `lease_min` (operator). |
| `POST /api/claims/{id}/bind` | Tie to a watched run: body `run_dir` (operator). |
| `POST /api/claims/{id}/release` | End a claim: body `reason`, `force` (operator; `force` for people only). |

## Charters

| Method and path | Returns |
|---|---|
| `GET /api/charters/profiles` | The charter profiles: name, plugin, accelerators, infra, time and idle limits, project. |
| `GET /api/charters/quote?profile=&project=&max_hours=&idle_min=` | Offers, cheapest first, the worst case, whether it would be pre-approved and if not why, and the project's usage (operator). Spends nothing. |
| `GET /api/charters?all=false` | Charters active or ended in the last day, or every one with `all=true`. |
| `GET /api/charters/{id}` | One charter. |
| `POST /api/charters` | Charter a yacht at the cheapest offer (operator). Body: `profile`, `project`, `max_hours`, `idle_min`, `note`, `approve` (purser only). Returns the charter with `why_not` when it waits for a purser. |
| `POST /api/charters/{id}/approve` | Approve a waiting charter (purser). |
| `GET /api/charters/{id}/log` | The end of a charter's set-up, sync and landing output (`?limit=` bytes, default 8000) and the step it is on now (operator). |
| `POST /api/charters/{id}/requote` | Quote a waiting (or approved, not yet launching) charter again for its machine (its requester or a purser). An approved charter whose rate rose goes back to a purser unless pre-approval still covers it. |
| `GET /api/purser` | What a purser decides on: waiting charters with `why_not`, `quoted_at` and the current `market` for their GPU, every budget, provider balances, and recent money events. |
| `POST /api/charters/{id}/reject` | Reject a waiting charter: body `reason` (purser). |
| `POST /api/charters/{id}/end` | Withdraw a charter, or land and destroy its yacht now: body `reason` (the requester, or a purser). |
| `GET /api/budgets` | Per project: limits, reserved spend per window, what is left, actual spend this month. |
| `PUT /api/budgets/{project}` | Change limits (purser): any of `monthly_usd`, `per_charter_usd`, `daily_usd`, `weekly_usd`, `pre_max_hours`, `pre_idle_min`. |
| `GET /api/prices` | Per watched GPU: providers with their cheapest region and price, whether they are launchable, and a 30-day trend; plus `run_rate` (`rate_usd_h`, `yachts`, `month_spent_usd`, `month_worst_case_usd`) and `sampled_at`. |
| `POST /api/prices/refresh` | Quote every watched GPU now (operator); 429 if the last sample is under 10 minutes old. |

`GET /api/state` also carries `charters` (active, or ended in the last day) and `budgets`. A
charter carries `id`, `profile`, `plugin`, `project`, `requester`, `state`, `approval`
(`pre-approved` or `purser`), `offer`, `rate_usd_h`, `max_hours`, `idle_min`, `worst_usd`,
`cost_so_far`, `cost_usd` (once ended), `ends_by`, `host`, `claims`, `end_reason`, `last_error`
and its timestamps.

## Times and claims

Times in the API are Unix seconds. A claim carries `id`, `resource`, `host`, `card`, `owner`,
`owner_kind`, `tier`, `state` (`queued`, `offered`, `held`, `released`, `expired`, `skipped`),
`note`, `share`, `est_hours`, `lease_s`, `pickup_s`, `run_key` and its timestamps. The field `card` is the
GPU's `nvidia-smi` index (empty for a whole-host claim).

## Shapes

A host, as `GET /api/hosts` returns it:

```json
{"name": "gpu-box", "transport": "ssh", "ssh": "gpu-box", "lifecycle": "static",
 "expires_at": null, "interval_s": 45, "accepts": [], "labels": {},
 "services": {"dashboard": "http://gpu-box.lan:9000"}, "health": {"dashboard": "/healthz"},
 "fallbacks": {"dashboard": ["http://gpu-box-vpn.lan:9000"]},
 "run_link": "{service:dashboard}/run/{name}", "added_at": 1790000000.0,
 "last_seen": 1790000040.0, "last_error": null,
 "service_health": {"dashboard": {"ok": true, "detail": "HTTP 200",
   "url": "http://gpu-box.lan:9000/healthz", "base": "http://gpu-box.lan:9000",
   "checked_at": 1790000041.0, "changed_at": 1790000000.5}},
 "services_active": {"dashboard": "http://gpu-box.lan:9000"}}
```

`base` is the address that answered (the service's own, or a fallback), and `services_active`
maps each service to the address to use now. Tools that open a host's services should use
`services_active`; `services` keeps each service's own address.

`changed_at` is when the service last went up or down; `detail` is the HTTP status, `no answer in
5 s`, or the connection error.

`GET /api/whoami`:

```json
{"identity": "sweep-agent-3", "kind": "agent", "roles": ["viewer", "operator"]}
```

## Example

```bash
curl -s localhost:8795/api/resources/gpu-box/gpu1 | jq '.holders[].owner, .free_at'
curl -s -X POST localhost:8795/api/claims \
  -H "Authorization: Bearer $LABTENDER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"resource": "gpu-box/gpu1", "est_hours": 2, "note": "ablation"}'
```
