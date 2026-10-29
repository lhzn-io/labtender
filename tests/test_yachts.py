"""The plugin base class: reading a file on a yacht over SSH."""

import subprocess

import pytest

from labtender.yachts import Plugin, PluginError, Yacht

YACHT = Yacht("lt-7", "lt-7", "ssh", "neocloud-a", "us-east-1")


def ran(monkeypatch, code, out="", err=""):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, code, out, err)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_a_missing_file_is_none_and_home_is_expanded_remotely(monkeypatch):
    calls = ran(monkeypatch, 3)
    assert Plugin().read_file(YACHT, "~/work done") is None
    assert '"$HOME"/\x27work done\x27' in calls[0][-1]


def test_a_present_file_is_read(monkeypatch):
    ran(monkeypatch, 0, "0\n")
    assert Plugin().read_file(YACHT, "/tmp/done") == "0\n"


def test_an_unreachable_yacht_is_an_error_not_an_answer(monkeypatch):
    ran(monkeypatch, 255, err="ssh: connect to host lt-7 port 22: Connection refused")
    with pytest.raises(PluginError, match="Connection refused"):
        Plugin().read_file(YACHT, "~/done")
