"""Yacht plugins: where chartered cloud GPUs come from.

A plugin turns a charter profile into offers (``quote``), creates and destroys machines, and
reports which of its machines exist. labtender's core owns every decision about money: it
approves charters against budgets, meters them, and calls ``destroy`` when the reaper says so. A
plugin never decides to spend.

Plugins are found by name: the built-in ones below, then the ``labtender.yachts`` entry point
group, so a separate package can add a provider.

Every method may block (cloud APIs are slow); labtender calls them in a worker thread.
"""

from __future__ import annotations

import importlib
import os
import shlex
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

READY_TRY_S = 30  # one "does it answer over SSH" attempt

BUILTIN = {
    "fake": "labtender.yachts.fake:FakePlugin",
    "skypilot": "labtender.yachts.skypilot:SkyPilotPlugin",
}


@dataclass
class Offer:
    """One way to fill a charter profile, with its price."""

    provider: str
    region: str
    gpu: str
    rate_usd_h: float
    detail: dict = field(default_factory=dict)


@dataclass
class Yacht:
    """A machine a plugin created: how to reach it, and the plugin's own name for it."""

    ref: str
    ssh: str | None
    transport: str = "ssh"  # ssh, or local for the fake plugin
    provider: str = ""
    region: str = ""
    detail: dict = field(default_factory=dict)


class PluginError(RuntimeError):
    pass


class Plugin:
    """Base class. Subclasses implement quote, create, destroy, status and clusters."""

    name = "base"

    def quote(self, spec: dict) -> list[Offer]:
        """Offers for a profile, cheapest first. Must not spend money."""
        raise NotImplementedError

    def create(self, ref: str, spec: dict, offer: Offer, max_hours: float) -> Yacht:
        """Create the machine named ``ref`` and return once it exists.

        It must also arrange, on the provider's side, for the machine to be destroyed after
        ``max_hours`` even if labtender never calls ``destroy``.
        """
        raise NotImplementedError

    def wait_ready(self, yacht: Yacht, timeout_s: float = 900) -> None:
        """Return once the machine answers over SSH."""
        if yacht.transport != "ssh":
            return
        deadline = time.monotonic() + timeout_s
        while True:
            # ConnectTimeout covers only the connection: a login that hangs after it (a broken
            # shell start-up file on the image) must not hold the charter in launching forever.
            try:
                r = subprocess.run(
                    [
                        *("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"),
                        *("--", yacht.ssh or "", "true"),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=READY_TRY_S,
                )
                ok, why = r.returncode == 0, r.stderr.strip()[-300:]
            except subprocess.TimeoutExpired:
                ok, why = False, f"no answer within {READY_TRY_S} s"
            if ok:
                return
            if time.monotonic() > deadline:
                raise PluginError(f"{yacht.ref} did not answer over SSH: {why}")
            time.sleep(10)

    def bootstrap(
        self,
        yacht: Yacht,
        commands: list,
        timeout_s: float = 1800,
        log: Path | None = None,
        progress: Callable | None = None,
    ) -> None:
        """Take the work out: run the profile's bootstrap commands."""
        run_commands(yacht, commands, timeout_s, log=log, progress=progress)

    def land(
        self,
        yacht: Yacht,
        commands: list,
        timeout_s: float = 900,
        log: Path | None = None,
        progress: Callable | None = None,
        values: dict | None = None,
    ) -> None:
        """Bring the results back: run the profile's land (or sync) commands."""
        run_commands(yacht, commands, timeout_s, log=log, progress=progress, values=values)

    def read_file(self, yacht: Yacht, path: str, timeout_s: float = 30) -> str | None:
        """A small file's contents on the machine, or None when it does not exist (yet).

        Raises PluginError when the machine cannot be asked, so an unanswered question is never
        taken for an answer. ``~/`` at the start of ``path`` is the remote user's home.
        """
        if yacht.transport != "ssh":
            local = Path(os.path.expanduser(path))
            return local.read_text(errors="replace")[:4096] if local.is_file() else None
        quoted = '"$HOME"/' + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)
        try:
            r = subprocess.run(
                [
                    *("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", yacht.ssh or ""),
                    f"if [ -f {quoted} ]; then head -c 4096 -- {quoted}; else exit 3; fi",
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            raise PluginError(f"{yacht.ref} did not answer within {timeout_s:g} s") from None
        if r.returncode == 3:
            return None
        if r.returncode != 0:
            raise PluginError(f"{yacht.ref}: {r.stderr.strip()[-300:] or f'exit {r.returncode}'}")
        return r.stdout

    def destroy(self, ref: str) -> None:
        """Destroy the machine. Must succeed quietly when it is already gone."""
        raise NotImplementedError

    def status(self, ref: str) -> str:
        """``up``, ``gone``, or another provider state."""
        raise NotImplementedError

    def clusters(self) -> list[str]:
        """Names of every machine this plugin can see, for the orphan sweep."""
        raise NotImplementedError

    def cost_so_far(self, ref: str) -> float | None:
        """The provider's own figure for what the machine has cost, when it has one."""
        return None

    def balances(self) -> dict[str, dict]:
        """Prepaid balances of the providers that have one and report it, by provider:
        ``{"balance_usd": ..., "spend_usd_h": ...}``. A provider missing here is not checked."""
        return {}


def step_parts(step) -> tuple[str, str | None]:
    """A profile step is a command, or a table ``{run = "...", label = "..."}`` whose label the
    board shows while it runs."""
    if isinstance(step, dict):
        return str(step["run"]), step.get("label")
    return str(step), None


def run_commands(
    yacht: Yacht,
    commands: list,
    timeout_s: float,
    log: Path | None = None,
    progress: Callable | None = None,
    values: dict | None = None,
) -> None:
    """Run profile commands on the labtender service's machine.

    ``{ssh}`` in a command becomes the yacht's SSH alias and ``{ref}`` its name, so a command
    can copy work out (``rsync -a work/ {ssh}:work/``) or run something there
    (``ssh {ssh} 'cd work && ./setup.sh'``).

    ``values`` adds placeholders of its own, each also set as ``LABTENDER_<NAME>`` in the
    commands' environment: landing passes ``charter``, ``cost_usd`` (so far) and ``rate_usd_h``,
    so a land step can record what the run cost next to its results.

    Each step's output (stdout and stderr together) is appended to ``log`` as it is written, so
    an operator can follow a long set-up; ``progress(step, steps, label)`` is called before each
    step. A failed step raises with an excerpt of its own output.
    """
    deadline = time.monotonic() + timeout_s
    path = Path(log) if log else None
    env = {**os.environ, **{f"LABTENDER_{k.upper()}": str(v) for k, v in (values or {}).items()}}
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
    for i, step in enumerate(commands, 1):
        cmd, label = step_parts(step)
        line = cmd.replace("{ssh}", shlex.quote(yacht.ssh or "")).replace(
            "{ref}", shlex.quote(yacht.ref)
        )
        for k, v in (values or {}).items():
            line = line.replace("{" + k + "}", shlex.quote(str(v)))
        left = deadline - time.monotonic()
        if left <= 0:
            raise PluginError(f"out of time before: {cmd}")
        if progress:
            progress(i, len(commands), label)
        # a+b: appended to, and read back from the step's start for an error excerpt.
        out = open(path, "a+b") if path else tempfile.TemporaryFile()  # noqa: SIM115
        with out:
            out.write(
                f"\n== step {i} of {len(commands)}{f': {label}' if label else ''}"
                f" ({time.strftime('%H:%M:%S')})\n".encode()
            )
            out.flush()
            start = out.tell()
            try:
                r = subprocess.run(
                    line, shell=True, stdout=out, stderr=subprocess.STDOUT, timeout=left, env=env
                )
            except subprocess.TimeoutExpired:
                raise PluginError(f"timed out: {cmd}") from None
            if r.returncode:
                out.seek(start)
                text = out.read().decode("utf-8", "replace")
                raise PluginError(f"exit {r.returncode} from: {cmd}\n{excerpt(text, 3000)}")


def excerpt(text: str | None, limit: int) -> str:
    """The start and the end of a long output: an error's message is usually near the top of a
    stack trace, and its last lines say where it stopped."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    return f"{text[:head]}\n[... {len(text) - limit} characters left out ...]\n{text[-tail:]}"


def load(name: str) -> Plugin:
    """An instance of the named plugin."""
    target = BUILTIN.get(name)
    if target is None:
        found = [ep for ep in entry_points(group="labtender.yachts") if ep.name == name]
        if not found:
            raise PluginError(f"no yacht plugin {name!r}")
        return found[0].load()()
    module, _, cls = target.partition(":")
    try:
        return getattr(importlib.import_module(module), cls)()
    except ImportError as e:
        raise PluginError(f"yacht plugin {name!r} is not installed: {e}") from None
