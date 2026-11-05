"use strict";
// The board's formatting helpers, and how a GPU's ledger, a run, a host's services and the
// yachts are drawn, kept apart from the page so other pages can share them. A page using them
// defines refresh().
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Only http(s) links are followed: a run link that says javascript: or data: would run in the
// browser of whoever clicks it.
const safeHref = (u) => (/^https?:\/\//i.test(String(u ?? "")) ? String(u) : "#");
const pct = (v) => (v == null ? null : Math.max(0, Math.min(100, v)));
function ago(t, now) {
  if (!t) return "never";
  const s = now - t;
  return s < 120 ? `${Math.round(s)} s ago` : s < 7200 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`;
}
function dur(s) {
  if (s == null) return "-";
  const m = Math.round(s / 60), h = Math.floor(m / 60);
  return h ? `${h} h ${String(m % 60).padStart(2, "0")} min` : `${m} min`;
}
function clock(t) {
  const d = new Date(t * 1000), now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === now.toDateString() ? hm : `${d.toLocaleDateString([], { weekday: "short" })} ${hm}`;
}
function bar(v, color) {
  const p = pct(v);
  return `<div class="bar"><i style="width:${p ?? 0}%;background:${color}"></i></div>`;
}
const openDetails = new Set();
document.addEventListener("toggle", (e) => {
  const k = e.target.dataset?.key;
  if (k) e.target.open ? openDetails.add(k) : openDetails.delete(k);
  if (e.target.open && e.target.dataset?.log) loadLog(e.target.dataset.log);
}, true);
// A charter's set-up, sync and landing output, fetched while its log panel is open. Operators only:
// the output can name private paths, so it needs a sign-in or a token (see auth.js).
const logs = {};
async function loadLog(id, ask = true) {
  const r = await ltFetch(`/api/charters/${id}/log?limit=6000`, {}, "operator", ask);
  if (!r.ok) {
    logs[id] = `log not available: ${(await r.json().catch(() => ({}))).detail || r.statusText}`;
  } else {
    logs[id] = (await r.json()).tail || "nothing logged yet";
  }
  const pre = document.querySelector(`pre[data-log-for="${id}"]`);
  if (pre) { pre.textContent = logs[id]; pre.scrollTop = pre.scrollHeight; }
}
function ledgerHtml(l, now) {
  if (!l) return "";
  const kind = (c) => (c.owner_kind === "agent" ? "agent" : "person");
  const holds = l.holders.map((c) => {
    const until = c.est_end ? ` until ${clock(c.est_end)} (${esc(c.est_source)})` : ", no estimate";
    const share = c.share ? ` · shared: ${esc(c.share)}` : "";
    const late = c.frozen ? ` <span class="wait">· paused: ${esc(c.frozen)}</span>`
      : c.late ? ` <span class="late">· heartbeat late</span>` : "";
    return `<div><span class="who">${esc(c.owner)}</span> (${kind(c)}, ${esc(c.tier)})${until}${share}${late}</div>`;
  }).join("");
  const free = l.holders.length ? "" : `<div>free</div>`;
  const queue = l.queue.map((c) => {
    const when = c.state === "offered" ? "offered now" : c.est_start ? `from ${clock(c.est_start)}` : "after an open-ended hold";
    return `${esc(c.owner)} (${esc(c.tier)}, ${when})`;
  }).join(", ");
  const flags = (l.flags || []).map((f) => `<div class="flag">${esc(f.kind.replace("-", " "))}: ${esc(f.detail)}</div>`).join("");
  return `<div class="ledger">${holds}${free}${queue ? `<div class="wait">waiting: ${queue}</div>` : ""}${flags}</div>`;
}
function windowsHtml(list) {
  if (!list || !list.length) return "";
  const gib = (m) => (m / 1024).toFixed(1);
  const parts = list.filter((p) => p.kind !== "desktop")
    .map((p) => `<b>${p.kind === "wsl" ? "WSL VM" : esc(p.name)}</b> ${Math.round(p.util_pct)} %, ${gib(p.mem_mib)} GiB`);
  const desk = list.filter((p) => p.kind === "desktop");
  if (desk.length) {
    const mem = desk.reduce((s, p) => s + (p.mem_mib || 0), 0);
    const top = desk.reduce((a, p) => (p.util_pct > (a?.util_pct ?? -1) ? p : a), null);
    parts.push(`${desk.length} desktop app${desk.length > 1 ? "s" : ""} ${gib(mem)} GiB` +
      (top && top.util_pct >= 1 ? ` (${esc(top.name)} ${Math.round(top.util_pct)} %)` : ""));
  }
  return `<div class="facts">Windows: ${parts.join(" · ")}</div>`;
}
function runHtml(r, now) {
  const p = r.pct;
  const where = r.card != null ? ` · GPU ${r.card}` : "";
  const t = r.t_h != null && r.target_h ? `${r.t_h.toFixed(1)} / ${r.target_h.toFixed(0)} sim h` : "";
  const rate = r.wall_min_per_sim_h ? ` · pace ${r.wall_min_per_sim_h.toFixed(1)} min per h` : "";
  const eta = r.state === "running" && r.finish_at ? ` · done ${clock(r.finish_at)} (in ${dur(r.finish_at - now)})` : "";
  const code = (r.exit_code != null ? ` · exit ${r.exit_code}` : "") + (r.failure ? ` · ${esc(r.failure)}` : "");
  return `<div class="run">
    <div class="top"><span class="name" title="${esc(r.dir)}">${r.link ? `<a href="${esc(safeHref(r.link))}" target="_blank" rel="noopener">${esc(r.name)}</a>` : esc(r.name)}</span><span class="badge s-${esc(r.state)}">${r.never_started ? "never started" : esc(r.state)}</span></div>
    <div class="row"><span class="lab">${p == null ? "?" : p.toFixed(0) + " %"}</span>${bar(p, "var(--accent)")}<span class="val">log ${ago(r.last_log_at, now)}</span></div>
    <div class="facts">${t}${rate}${eta}${code}${where}</div>
  </div>`;
}
function servicesHtml(h, now) {
  const checks = h.service_health || {};
  const names = Object.keys(h.services || {}).sort();
  if (!names.length) return "";
  const parts = names.map((n) => {
    const c = checks[n], url = (h.services_active || {})[n] || h.services[n];
    const name = `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(n)}</a>`;
    if (!c) return name;
    const via = c.ok && c.base && c.base !== h.services[n] ? ` via ${esc(new URL(c.base).host)}` : "";
    // An optional service is down at times by design, and a service on a host that is not
    // answering is down with it (the host is the alert): off, in muted ink, not a fault.
    const off = (h.optional || []).includes(n) ? "optional" : !h.online ? "the host is not answering" : null;
    return c.ok
      ? `${name} <span class="up" title="${esc(c.detail)}, checked ${ago(c.checked_at, now)}">up${via}</span>`
      : off
        ? `${name} <span class="off" title="${off}; ${esc(c.detail)}">off</span>`
        : `${name} <span class="down" title="${esc(c.url)}">down since ${clock(c.changed_at)}: ${esc(c.detail)}</span>`;
  });
  return `<div class="svcs">services: ${parts.join(" · ")}</div>`;
}
// Bytes per second as the network rate a person reads: kB/s, MB/s.
function rate(bps) {
  return bps >= 1e6 ? `${(bps / 1e6).toFixed(1)} MB/s` : bps >= 1e3 ? `${(bps / 1e3).toFixed(0)} kB/s` : `${bps} B/s`;
}
const money = (v) => (v == null ? "-" : `$${Number(v).toFixed(2)}`);
function charterHtml(c, now) {
  const o = c.offer || {};
  const live = ["live", "landing", "adrift", "launching"].includes(c.state);
  const facts = [
    `${esc(o.provider)} ${esc(o.region)}`, esc(c.accelerators || o.gpu || ""),
    `${money(c.rate_usd_h)}/h`, `up to ${c.max_hours} h (worst ${money(c.worst_usd)})`,
    `idle limit ${c.idle_min} min`,
    live ? `${money(c.cost_so_far)} so far` : c.cost_usd != null ? `cost ${money(c.cost_usd)}` : null,
    live && c.ends_by ? `ends by ${clock(c.ends_by)}` : null,
    c.host ? `host ${esc(c.host)}` : null,
  ].filter(Boolean).join(" · ");
  const why = c.state === "requested" ? `<div class="wait">waiting for a purser: labtender charter approve ${c.id}</div>
    <button data-act="approve" data-id="${c.id}">Approve</button><button data-act="reject" data-id="${c.id}">Reject</button>` : "";
  const adrift = c.state === "adrift" ? `<div class="adrift">destroy failed, retrying: ${esc(summarize(c.last_error))}</div>` : "";
  const ended = !live && c.end_reason ? ` · ${esc(summarize(c.end_reason))}` : "";
  const err = cleanError(c.last_error);
  const key = `charter-error:${c.id}`;
  const detail = err && summarize(err) !== err
    ? `<details data-key="${esc(key)}"${openDetails.has(key) ? " open" : ""}><summary class="sub">error details</summary><div class="detail">${esc(err)}</div></details>` : "";
  const p = c.progress;
  const step = p ? `<div class="step">${esc(p.phase)}: step ${p.step} of ${p.steps}${p.label ? `, ${esc(p.label)}` : ""} · this step ${dur(now - p.step_at)}, ${esc(p.phase)} ${dur(now - p.phase_at)} so far</div>` : "";
  const lkey = `charter-log:${c.id}`;
  const log = c.ref ? `<details data-key="${esc(lkey)}" data-log="${c.id}"${openDetails.has(lkey) ? " open" : ""}><summary class="sub">log</summary><pre class="detail" data-log-for="${c.id}">${esc(logs[c.id] ?? "loading...")}</pre></details>` : "";
  return `<div class="charter">
    <div class="top"><span><b>charter ${c.id}</b> ${esc(c.profile)} for ${esc(c.requester)}${c.approval ? ` (${esc(c.approval)})` : ""}</span><span class="badge s-${esc(c.state)}">${esc(c.state)}</span></div>
    <div class="facts">${facts}${ended}</div>${c.note ? `<div class="facts">${esc(c.note)}</div>` : ""}${step}${adrift}${why}${detail}${log}
  </div>`;
}
// Provider errors arrive with terminal colour codes and pages of retry detail; the board shows
// one line (capacity shortages by zone) and keeps the rest a click away.
const cleanError = (t) => String(t ?? "").replace(/\x1b\[[0-9;]*[A-Za-z]/g, "").replace(/\[[0-9;]*m/g, "").trim();
function summarize(t) {
  const text = cleanError(t);
  const prefix = text.startsWith("launch failed: ") ? "launch failed: " : "";
  const zones = [...new Set([...text.matchAll(/Failed to acquire resources in (\S+) for/g)].map((m) => m[1]))];
  if (zones.length || ["RESOURCE_POOL_EXHAUSTED", "Failed to provision all possible launchable resources"].some((w) => text.includes(w))) return `${prefix}no capacity${zones.length ? " in " + zones.join(", ") : ""}`;
  const first = text.split("\n").find((l) => l.trim()) || "";
  return first.length > 160 ? first.slice(0, 157) + "..." : first;
}
function budgetHtml(u) {
  const r = u.reserved, l = u.limits;
  return `<div class="budget">${esc(u.project)}: month ${money(r.monthly_usd)} of ${money(l.monthly_usd)} reserved (${money(u.month_actual_usd)} spent) · pre-approved today ${money(r.daily_usd)} of ${money(l.daily_usd)}, this week ${money(r.weekly_usd)} of ${money(l.weekly_usd)}, up to ${money(l.per_charter_usd)} each</div>`;
}
const ACTIVE_CHARTER = ["requested", "approved", "launching", "live", "landing", "adrift"];
function yachtsHtml(s) {
  const charters = s.charters || [];
  if (!charters.length) return "";
  const active = charters.filter((c) => ACTIVE_CHARTER.includes(c.state));
  const ended = charters.filter((c) => !ACTIVE_CHARTER.includes(c.state));
  const spent = ended.reduce((t, c) => t + (c.cost_usd || 0), 0);
  const key = "ended:charters";
  const endedHtml = ended.length
    ? `<details data-key="${key}"${openDetails.has(key) ? " open" : ""}><summary class="sub">${ended.length} ended charter${ended.length > 1 ? "s" : ""} in the last day, ${money(spent)}</summary>${ended.map((c) => charterHtml(c, s.time)).join("")}</details>`
    : "";
  const none = active.length ? "" : `<div class="empty">No yachts out.</div>`;
  const r = s.run_rate;
  const rate = r && r.yachts ? `<div class="budget">now ${money(r.rate_usd_h)}/h for ${r.yachts} yacht${r.yachts > 1 ? "s" : ""} · <a href="/prices">prices</a></div>` : "";
  // A prepaid provider stops its machines (and loses their disks) when the balance runs out.
  const bal = (s.providers || []).map((p) => `<div class="budget${p.low ? " wait" : ""}">${esc(p.provider)} balance ${money(p.balance_usd)}${p.committed_usd ? ` · active yachts may still cost ${money(p.committed_usd)}` : ""}${p.low ? " · <b>too low: top it up</b>" : ""}</div>`).join("");
  return `<section class="host yachts"><h2 class="label">Yachts</h2>${rate}${bal}${(s.budgets || []).map(budgetHtml).join("")}${none}${active.map((c) => charterHtml(c, s.time)).join("")}${endedHtml}</section>`;
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const body = b.dataset.act === "reject" ? JSON.stringify({ reason: prompt("Reason for rejecting:") || "" }) : undefined;
  const r = await ltFetch(`/api/charters/${b.dataset.id}/${b.dataset.act}`, { method: "POST", body }, "purser");
  if (!r.ok) {
    alert(`${b.dataset.act} failed: ${(await r.json()).detail}`);
  }
  refresh();
});
