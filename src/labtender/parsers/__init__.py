"""Run-log parser specs: turn a run folder's log tail and marker files into progress and an ETA.

A spec is a TOML file. Built-in specs ship in this package; a lab adds its own under
``$LABTENDER_HOME/parsers/``, where a spec of the same name overrides the built-in one.
See ``run-wrapper.toml`` for the annotated format.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from importlib import resources
from pathlib import Path

UNIT_HOURS = {
    "s": 1 / 3600,
    "sec": 1 / 3600,
    "seconds": 1 / 3600,
    "min": 1 / 60,
    "minutes": 1 / 60,
    "h": 1.0,
    "hr": 1.0,
    "hours": 1.0,
    "d": 24.0,
    "day": 24.0,
    "days": 24.0,
}


def to_hours(value: float, unit: str) -> float:
    try:
        return value * UNIT_HOURS[unit.lower()]
    except KeyError:
        raise ValueError(f"unknown time unit {unit!r}") from None


def load_specs(home: Path | None = None) -> dict[str, dict]:
    specs: dict[str, dict] = {}
    for f in resources.files(__package__).iterdir():
        if f.name.endswith(".toml"):
            spec = tomllib.loads(f.read_text(encoding="utf-8"))
            specs[spec["name"]] = spec
    if home is not None and (home / "parsers").is_dir():
        for f in sorted((home / "parsers").glob("*.toml")):
            spec = tomllib.loads(f.read_text(encoding="utf-8"))
            specs[spec["name"]] = spec
    return specs


def probe_request(spec: dict, glob: str) -> dict:
    """The part of the probe arguments that asks for this spec's run folders."""
    markers = list(spec.get("markers", []))
    for key in ("marker", "file"):
        done = spec.get("done", {}).get(key)
        if done and done not in markers:
            markers.append(done)
    return {
        "glob": glob,
        "files": [spec["log"]],
        "markers": markers,
        "max_age_h": spec.get("max_age_h", 72),
        "max_dirs": spec.get("max_dirs", 8),
        "tail_bytes": spec.get("tail_bytes", 65536),
        "head_bytes": spec.get("head_bytes", 0),
    }


def _last_progress(spec: dict, text: str) -> tuple[dict | None, str | None]:
    prog = spec.get("progress", {})
    lines = text.splitlines()
    if prog.get("format", "regex") == "jsonl":
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and prog.get("t_field") in rec:
                return {
                    "t": rec.get(prog["t_field"]),
                    "t_unit": rec.get(prog.get("t_unit_field", ""), prog.get("t_unit", "h")),
                    "rate": rec.get(prog.get("rate_field", "")),
                    "iter": rec.get(prog.get("iter_field", "")),
                }, line
        return None, None
    rx = re.compile(prog["regex"])
    for line in reversed(lines):
        m = rx.search(line)
        if m:
            return m.groupdict(), line
    return None, None


def _target_hours(spec: dict, entry: dict) -> float | None:
    for t in spec.get("target", []):
        unit = t.get("unit", "h")
        if "marker" in t:
            text = entry.get("markers", {}).get(t["marker"])
            m = re.search(t["regex"], text or "")
        elif "dirname" in t:
            m = re.search(t["dirname"], os.path.basename(entry["dir"].rstrip("/")))
        elif "head" in t:
            head = (entry.get("files", {}).get(spec["log"]) or {}).get("head", "")
            m = re.search(t["head"], head)
        elif "log" in t:
            log = (entry.get("files", {}).get(spec["log"]) or {}).get("tail", "")
            m = re.search(t["log"], log)
        else:
            m = None
        if m:
            try:
                return to_hours(float(m.group("value")), unit)
            except (ValueError, IndexError):
                continue
    return None


def interpret(spec: dict, entry: dict, probe_time: float, hub_time: float) -> dict:
    """Progress, state and ETA of one run folder from a probe entry.

    Times in the probe are the node's clock; ages are taken against the probe's own
    ``time`` and re-based on the labtender service's clock, so node clock skew does not move ETAs.
    """
    log = entry.get("files", {}).get(spec["log"]) or {}
    text = log.get("tail", "")
    log_age = probe_time - log["mtime"] if log.get("mtime") is not None else None
    markers = entry.get("markers", {})
    out: dict = {
        "dir": entry["dir"],
        "name": os.path.basename(entry["dir"].rstrip("/")),
        "parser": spec["name"],
        "log_age_s": log_age,
        "last_log_at": hub_time - log_age if log_age is not None else None,
        "t_h": None,
        "target_h": None,
        "pct": None,
        "iter": None,
        "wall_min_per_sim_h": None,
        "eta_s": None,
        "finish_at": None,
        "exit_code": None,
        "card": None,
        "last_line": None,
    }

    rec, line = _last_progress(spec, text)
    if line is not None:
        out["last_line"] = line.strip()[-300:]
    prog = spec.get("progress", {})
    if rec:
        try:
            unit = rec.get("t_unit") or prog.get("t_unit", "h")
            out["t_h"] = to_hours(float(rec["t"]), unit)
        except (TypeError, ValueError, KeyError):
            pass
        if rec.get("iter") not in (None, ""):
            try:
                out["iter"] = int(float(rec["iter"]))
            except ValueError:
                pass
        if rec.get("rate") not in (None, ""):
            rate = float(rec["rate"])
            kind = prog.get("rate", "wall_min_per_sim_h")
            if kind == "wall_min_per_sim_h":
                out["wall_min_per_sim_h"] = rate
            elif kind == "sim_h_per_wall_h" and rate > 0:
                out["wall_min_per_sim_h"] = 60.0 / rate
            else:
                raise ValueError(f"unknown rate kind {kind!r}")

    out["target_h"] = _target_hours(spec, entry)
    if out["t_h"] is not None and out["target_h"]:
        out["pct"] = max(0.0, min(100.0, 100.0 * out["t_h"] / out["target_h"]))

    card = spec.get("card")
    if card:
        m = re.search(card["regex"], markers.get(card["marker"], ""))
        if m:
            out["card"] = int(m.group("card"))

    done = spec.get("done", {})
    code_text = markers.get(done.get("marker", "")) if done.get("marker") else None
    if code_text is not None and code_text.strip().lstrip("-").isdigit():
        out["exit_code"] = int(code_text.strip())
        out["state"] = "done" if out["exit_code"] == 0 else "exited"
    elif done.get("line") and text.rstrip().endswith(done["line"]):
        out["state"] = "done"
    elif done.get("file") and done["file"] in markers:
        # The run wrote its own completion file: finished, even if its last progress line came
        # a little before the end.
        out["state"] = "done"
        out["pct"] = 100.0
    elif failed := _failure(spec, text):
        # The log says the run failed (a crash or an error it stopped on): it has ended, and no
        # exit status will follow, so it is exited rather than left to go stale.
        out["state"] = "exited"
        out["failure"] = failed
    elif (
        done.get("at_target", True)
        and out["t_h"] is not None
        and out["target_h"]
        and out["t_h"] >= out["target_h"] - 1e-6
    ):
        out["state"] = "done"
    elif log_age is None or log_age > 60 * spec.get("stale_after_min", 30):
        # Progress lines come every N iterations, so a run that ended at its target may last
        # report a little short of it.
        near = out["pct"] is not None and out["pct"] >= done.get("near_target_pct", 99.0)
        out["state"] = "done" if done.get("at_target", True) and near else "stale"
    else:
        out["state"] = "running"

    if (
        out["state"] == "running"
        and out["t_h"] is not None
        and out["target_h"]
        and out["wall_min_per_sim_h"]
    ):
        remaining_h = max(0.0, out["target_h"] - out["t_h"])
        out["eta_s"] = remaining_h * out["wall_min_per_sim_h"] * 60.0
        out["finish_at"] = out["last_log_at"] + out["eta_s"]
    return out


def _failure(spec: dict, text: str) -> str | None:
    """The last log line matching the spec's [failed] regex, or None."""
    pattern = (spec.get("failed") or {}).get("regex")
    if not pattern:
        return None
    hits = [ln for ln in text.splitlines() if re.search(pattern, ln)]
    return hits[-1].strip()[:200] if hits else None
