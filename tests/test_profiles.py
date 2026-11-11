"""Profile files: reading, creating, editing and deleting them, and who may."""

import asyncio
import stat

import pytest
from fastapi.testclient import TestClient

from labtender.charter import CharterError, Charters, text_sha
from labtender.db import Store
from labtender.ledger import Ledger
from labtender.server import create_app, token_hash
from labtender.yachts.fake import FakePlugin

SMALL = 'plugin = "fake"\naccelerators = "L4:1"\nmax_hours = 4\nidle_min = 20\n'
PURSER = ("dana", "person", ["viewer", "operator", "admin", "purser"])


@pytest.fixture
def lab(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    ch = Charters(store, Ledger(store), tmp_path, plugins={"fake": FakePlugin()})
    return ch, store, tmp_path


def test_create_edit_and_keep_the_old_version(lab):
    ch, store, home = lab
    r = ch.save_profile("small", SMALL, "dana", None)
    assert r["created"] and ch.profile("small")["max_hours"] == 4
    path = home / "charters" / "small.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(CharterError, match="already exists"):
        ch.save_profile("small", SMALL, "dana", None)
    src = ch.profile_source("small")
    r = ch.save_profile("small", SMALL.replace("4", "2"), "dana", src["sha256"])
    assert not r["created"] and ch.profile("small")["max_hours"] == 2
    kept = list((home / "charters" / ".history").glob("small.*.toml"))
    assert len(kept) == 1 and kept[0].read_text() == SMALL
    assert [e["action"] for e in store.events(2)] == ["profile.save", "profile.save"]


def test_a_change_made_meanwhile_is_never_overwritten(lab):
    ch, store, home = lab
    ch.save_profile("small", SMALL, "dana", None)
    stale = text_sha(SMALL)
    ch.save_profile("small", SMALL + 'tier = "critical"\n', "lee", stale)
    with pytest.raises(CharterError, match="changed meanwhile"):
        ch.save_profile("small", SMALL, "dana", stale)


@pytest.mark.parametrize(
    "text, why",
    [
        ("plugin = ", "not valid TOML"),
        ('name = "other"\n' + SMALL, "must match"),
        ('plugin = "nope"\n', "no yacht plugin"),
        (SMALL.replace("idle_min = 20", "idle_min = 1"), "idle_min"),
        ("", "plugin is required"),
    ],
)
def test_a_profile_that_would_not_load_is_refused(lab, text, why):
    ch, store, home = lab
    with pytest.raises(CharterError, match=why):
        ch.save_profile("small", text, "dana", None)
    assert not (home / "charters" / "small.toml").exists()


@pytest.mark.parametrize("name", ["../x", "Big", "", "a/b", ".hidden", "x" * 65])
def test_names_stay_inside_the_profile_folder(lab, name):
    ch, store, home = lab
    with pytest.raises(CharterError, match="a profile name"):
        ch.save_profile(name, SMALL, "dana", None)


def test_delete_keeps_a_copy_and_waits_for_its_charters(lab):
    ch, store, home = lab
    ch.save_profile("small", SMALL, "dana", None)
    q = asyncio.run(ch.quote("small"))
    c = ch.request("small", q["offers"][0], *PURSER)
    with pytest.raises(CharterError, match=f"charters {c['id']} from profile small"):
        ch.delete_profile("small", "dana")
    assert ch.profile_files()[0]["in_use"] == [c["id"]]
    ch.end(c["id"], *PURSER)
    r = ch.delete_profile("small", "dana")
    assert r["kept"].endswith(".deleted.toml") and "small" not in ch.profiles()
    # The ended charter still has its profile as it was requested.
    assert ch.charter(c["id"])["spec"]["max_hours"] == 4


def test_a_broken_file_is_listed_so_it_can_be_fixed(lab):
    ch, store, home = lab
    ch.save_profile("small", SMALL, "dana", None)
    (home / "charters" / "broken.toml").write_text("plugin = \n")
    rows = {r["name"]: r for r in ch.profile_files()}
    assert rows["small"]["ok"] and "bootstrap" not in rows["small"]
    assert not rows["broken"]["ok"] and "not valid TOML" in rows["broken"]["error"]
    # The broken file is left out; every other profile still loads.
    assert list(ch.profiles()) == ["small"]
    src = ch.profile_source("broken")
    ch.save_profile("broken", SMALL, "dana", src["sha256"])
    assert all(r["ok"] for r in ch.profile_files())


def test_http_profiles_are_for_people_with_the_admin_role(lab):
    ch, store, home = lab
    store.add_token(
        token_hash("agent"),
        "sweep-agent",
        "agent",
        actor="a",
        roles=["viewer", "operator", "admin"],
    )
    store.add_token(token_hash("viewer"), "lee", "person", actor="a", roles=["viewer", "operator"])
    store.add_token(token_hash("person"), "dana", "person", actor="a")
    app = create_app(store=store, home=home, collect=False, ledger=ch.ledger, charters=ch)
    agent, viewer, person = (
        {"Authorization": f"Bearer {t}"} for t in ("agent", "viewer", "person")
    )
    with TestClient(app) as http:
        assert http.get("/profiles").status_code == 200
        body = {"toml": SMALL}
        for h in (agent, viewer):
            assert http.put("/api/charters/profiles/small", json=body, headers=h).status_code == 403
            assert http.get("/api/charters/profile-files", headers=h).status_code == 403
        r = http.put("/api/charters/profiles/small", json=body, headers=person)
        assert r.status_code == 200 and r.json()["created"]
        assert (
            http.put("/api/charters/profiles/small", json=body, headers=person).status_code == 409
        )
        src = http.get("/api/charters/profiles/small/source", headers=person).json()
        assert src["toml"] == SMALL
        assert http.get("/api/charters/profiles/small/source", headers=agent).status_code == 403
        # The open listing never shows a profile's commands.
        assert "bootstrap" not in http.get("/api/charters/profiles").json()[0]
        assert http.delete("/api/charters/profiles/small", headers=agent).status_code == 403
        assert http.delete("/api/charters/profiles/small", headers=person).status_code == 200
        assert http.get("/api/charters/profile-files", headers=person).json() == []
