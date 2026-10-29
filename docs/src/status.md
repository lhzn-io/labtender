# Status and roadmap

labtender is pre-alpha: it runs a real mixed lab, but interfaces may still change.

## Working

- Host registry with static and ephemeral hosts.
- Agentless probes for NVIDIA discrete GPUs, WSL2 (with per-GPU process placement), Jetson
  (Orin and Thor) and Apple silicon.
- Run following through parser specs, with progress, wall rate and estimated finish.
- Board and compact view.
- Claim ledger: queue with tiers, offers, leases, run binding, sharing agreements, estimated
  returns, reconciliation flags and an append-only audit trail.
- CLI, HTTP API, MCP server for agent sessions, and `preflight` for job wrappers.
- Per-process GPU use from the Windows side of WSL hosts, `all-smi` as a probe source where it
  is installed, and a Prometheus `/metrics` endpoint.
- Host services and run links for tools built beside labtender, and token roles.
- Claims that pause while their host or GPU is away, daily backups, and service units for
  systemd and launchd.
- Charters for cloud GPUs (yachts): worst-case pricing, a monthly cap, pre-approval within
  per-charter, daily and weekly limits, purser approval for the rest, a reaper for idle,
  expired and over-budget yachts, adrift retries and an orphan sweep, with a `fake` plugin for
  practice.
- A prices page: current quotes per watched GPU and provider, 30-day price trends from the
  service's own samples, and the run rate of live yachts.
- A SkyPilot yacht plugin for Google Cloud, AWS and GPU clouds such as Lambda, RunPod, Vast and
  DigitalOcean, with catalog quotes before any cloud is set up.
- Real charters on Google Cloud (L4) and RunPod (H100, H200, RTX PRO 6000), with fresh quotes
  before launch, prepaid balance checks, lost-yacht detection, landing retries, periodic result
  sync, a purser page, and set-up progress with a log tail on the board.
- Sign-in through an authenticating proxy (oauth2-proxy with any OpenID Connect provider), with
  an allow-list for roles; tokens stay for agents and the CLI.

## Next

- Later plugins: serverless GPUs (for example Modal) and dstack.
- Packaging on PyPI, so `uvx labtender mcp` works without a checkout, and an optional container
  image for the labtender service.

## Related projects

labtender reuses what it can and fills the gap between them:

| Project | What it covers |
|---|---|
| all-smi | Multi-vendor GPU metrics, including Jetson and Apple silicon. |
| homelab-monitor | An agentless SSH board with a read-only MCP server. |
| canhazgpu, GPUbk, TensorHive | GPU reservations, each for a single host, with a per-host install, or no longer maintained. |
| SkyPilot, dstack | Cloud launchers; SkyPilot is the first yacht plugin, dstack a candidate. |
| Prometheus with the DCGM exporter | Datacentre GPU metrics; not supported on WSL2 or Jetson. |
