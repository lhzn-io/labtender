# Safety and trust

labtender watches machines other people are using for long, expensive work. These are the
promises it keeps, and the assumptions it makes.

## What it does on a node

- It runs one short `python3` process per poll, at most every 30 s, reading `/proc`, sysfs,
  `nvidia-smi`, one `tegrastats` line or `ioreg`, and the tails of watched logs.
- It writes nothing on the node, installs nothing, and never asks for root or sudo.
- It never starts, stops, signals, pins or reprioritises a process.
- It inspects only processes owned by the SSH user; others are counted, not read.

## What the ledger can and cannot do

Claims are advisory. A claim does not stop anyone from using a GPU; it tells everyone, people
and agents alike, that the GPU is spoken for. Reconciliation flags make mismatches visible. The
enforcement is social, plus whatever your job wrappers do with `labtender preflight`.

## Access

- The labtender service binds to `127.0.0.1` by default. Bind it to a LAN or VPN address only, behind your
  firewall; it is not meant for the open internet.
- Reads (`GET`) need no token: anyone who can reach the labtender service can see the fleet, including process
  command lines and run folder paths.
- Changes need a bearer token with the right role: `operator` for claims and charter requests,
  `admin` for the host registry, `purser` (people only) for approving charters and setting
  budgets. Tokens are shown once at creation and stored as SHA-256 hashes; revoke them with
  `labtender token revoke`. Give view-only users a `viewer` token. Behind an authenticating proxy,
  people sign in instead and roles come from an allow-list (see [](sign-in.md)).
- The board keeps a token that works in the browser's local storage, so a person pastes it
  once per browser. Anyone using that browser account can act with it: on a shared computer,
  use *forget token* when done. A revoked token is dropped the next time it is refused.
- Agents cannot force-release someone else's claim; people can, with a recorded reason.

## Money

Cloud charters spend real money, so the rules for them are strict (see [](yachts.md)):

- Every charter is priced at its worst case, and nothing is approved that would pass the
  project's monthly cap.
- Agents can only charter within the pre-approval limits a purser set; anything bigger waits for
  a person with the `purser` role, a role agents can never hold.
- Two layers end every yacht: labtender's reaper (idle, time limit, budget) and a timer on the
  provider's side set at create, so a yacht dies on time even if labtender is down.
- A failed destroy is retried every minute and shown in red; a daily sweep destroys machines that
  carry this service's prefix but have no charter, and never touches anyone else's.
- labtender never creates cloud accounts or credentials; the plugins use whatever the service's
  machine is already signed in to.

## Audit trail

Host and watch changes, token creation, roles and revocation, every claim transition, every
charter transition and budget change, every run state change and every flag are appended to the `event` table. Database triggers refuse updates and
deletes, so the trail can only grow.

## SSH

The labtender service uses your own `ssh` and `~/.ssh/config`, with `BatchMode=yes` so it never prompts, and
shares one connection per host (`ControlMaster`) with sockets under `$LABTENDER_HOME/ssh/`.
Give the labtender service a key that can only do what a normal user can on each node.
