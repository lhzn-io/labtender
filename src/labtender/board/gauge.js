"use strict";
// Round gauges, shared by the board and the compact view. A 270 degree dial: frac in [0, 1] (null
// when the GPU does not report it), the value and unit in the middle, and (full size only) the
// label and its ceiling underneath. Near a ceiling the label turns amber and the line under it
// says so ("near full"); at it, the arc turns brick red too. Amber stays off the arcs, where it would read as the
// gold load line. Within limits power and temperature stay neutral.
const gaugeLevel = (f) => f == null ? "" : f >= 0.92 ? "bad" : f >= 0.78 ? "warn" : "";
const gaugeTone = (cls, normal) => cls === "bad" ? "var(--bad)" : normal;
const gaugeWord = (cls, warn, bad) => cls === "bad" ? bad : cls === "warn" ? warn : "";
const HOT_C = 80, VERY_HOT_C = 90;  // temperature words; the arc stays neutral below these
const tempLevel = (t) => t == null ? "" : t >= VERY_HOT_C ? "bad" : t >= HOT_C ? "warn" : "";

function dial({ value, unit, frac, color, label, of, cls = "", mini = false, title = "" }) {
  const gesc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const r = 46, cx = 60, cy = 60, start = 135, sweep = 270;
  const f = frac == null ? 0 : Math.max(0, Math.min(1, frac));
  const pt = (a) => [cx + r * Math.cos(a * Math.PI / 180), cy + r * Math.sin(a * Math.PI / 180)];
  const arc = (a0, a1) => {
    const [x0, y0] = pt(a0), [x1, y1] = pt(a1);
    return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  };
  const end = start + sweep * f, [nx, ny] = pt(end);
  const shown = frac != null && f > 0.002;
  const stroke = mini ? 13 : 9;
  const ticks = mini ? "" : Array.from({ length: 11 }, (_, i) => {
    const a = (start + sweep * i / 10) * Math.PI / 180, r0 = i % 5 ? 53 : 51, r1 = 56;
    return `<line x1="${(cx + r0 * Math.cos(a)).toFixed(1)}" y1="${(cy + r0 * Math.sin(a)).toFixed(1)}" x2="${(cx + r1 * Math.cos(a)).toFixed(1)}" y2="${(cy + r1 * Math.sin(a)).toFixed(1)}" stroke="var(--line)" stroke-width="${i % 5 ? 1 : 1.6}"/>`;
  }).join("");
  const svg = `<svg viewBox="0 0 120 ${mini ? 120 : 106}" role="img" aria-label="${gesc(label)}: ${gesc(value)} ${gesc(unit)}">
      ${ticks}
      <path d="${arc(start, start + sweep)}" stroke="var(--track)" stroke-width="${stroke}" fill="none" stroke-linecap="round"/>
      ${shown ? `<path d="${arc(start, end)}" stroke="${color}" stroke-width="${stroke}" fill="none" stroke-linecap="round"/>` : ""}
      ${frac != null && !mini ? `<circle cx="${nx.toFixed(2)}" cy="${ny.toFixed(2)}" r="3.2" fill="var(--panel)" stroke="${color}" stroke-width="2"/>` : ""}
      <text x="60" y="${mini ? 71 : 66}" text-anchor="middle" class="v">${gesc(value)}</text>
      ${mini ? "" : `<text x="60" y="80" text-anchor="middle" class="u">${gesc(unit)}</text>`}
    </svg>`;
  if (mini) return `<span class="mini" title="${gesc(title || `${label} ${value}${unit ? " " + unit : ""}`)}">${svg}</span>`;
  return `<div class="dial">${svg}<div class="lbl ${cls}">${gesc(label)}</div><div class="of ${cls}">${gesc(of || "")}</div></div>`;
}

// A GPU's four gauges at full size, or three (load, memory, temperature) small ones.
function gpuDials(g, mini = false) {
  const gib = (mib) => (mib / 1024).toFixed(1);
  const memF = g.mem_total_mib ? (g.mem_used_mib ?? 0) / g.mem_total_mib : null;
  const pwrF = g.power_w != null && g.power_limit_w ? g.power_w / g.power_limit_w : null;
  const t = g.temp_c, tcls = tempLevel(t);
  const tword = tcls === "bad" ? "very hot" : tcls === "warn" ? "hot" : "";
  const memLabel = g.unified ? "memory" : "VRAM";
  const load = dial({ mini, label: "load", value: g.util_pct == null ? "-" : Math.round(g.util_pct), unit: g.util_pct == null ? "" : "%",
    frac: g.util_pct == null ? null : g.util_pct / 100, color: "var(--util)", of: g.clock_mhz != null ? `${g.clock_mhz} MHz` : "" });
  const mem = dial({ mini, label: memLabel, value: memF == null ? "-" : mini ? Math.round(100 * memF) : gib(g.mem_used_mib ?? 0),
    unit: memF == null ? "" : mini ? "%" : "GiB", frac: memF, color: gaugeTone(gaugeLevel(memF), "var(--mem)"),
    of: memF == null ? "" : gaugeWord(gaugeLevel(memF), "near full", "full") || `of ${gib(g.mem_total_mib)} GiB`, cls: gaugeLevel(memF),
    title: memF == null ? "" : `${memLabel} ${gib(g.mem_used_mib ?? 0)} of ${gib(g.mem_total_mib)} GiB` });
  const temp = dial({ mini, label: "temp", of: tword, value: t == null ? "-" : Math.round(t), unit: t == null ? "" : "C",
    frac: t == null ? null : t / 100, color: gaugeTone(tcls, "var(--neutral)"), cls: tcls });
  if (mini) return load + mem + temp;
  const power = dial({ label: "power", value: g.power_w == null ? "-" : Math.round(g.power_w), unit: g.power_w == null ? "" : "W",
    frac: pwrF, color: gaugeTone(gaugeLevel(pwrF), "var(--neutral)"), of: gaugeWord(gaugeLevel(pwrF), "near limit", "at limit") || (g.power_limit_w ? `limit ${Math.round(g.power_limit_w)} W` : ""),
    cls: gaugeLevel(pwrF) });
  return load + mem + power + temp;
}
