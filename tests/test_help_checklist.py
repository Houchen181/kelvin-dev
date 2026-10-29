"""Help in the portal, from Kelvin's own documentation, and a first-run checklist: lab set up, instruments checked,
limits set, owner PIN, phone paired, data folder (F3 #20: no in-app help at all)."""
import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

from labharness import build_virtual_lab, guide  # noqa: E402
from labharness.agent.approval import WebApprover  # noqa: E402
from labharness.agent.runtime import LabRuntime  # noqa: E402
from labharness.web.app import create_app  # noqa: E402
from labharness.web.labops import checklist  # noqa: E402
from labharness.web.settings import Settings  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402


def test_help_finds_the_passages_people_read_and_whole_sections():
    found = guide.lookup("owner PIN")
    assert found and any("PIN" in p["text"] for p in found)
    assert not any(p["where"].startswith("the portal's map") for p in found)   # that one is written for models
    topics = guide.topics()
    assert {"doc": "Using Kelvin", "title": "Terminal", "where": "portal.md › Terminal"} in topics
    assert "/use lockin" in guide.section("portal.md › Terminal")
    assert guide.section("nowhere.md › Terminal") == "" and guide.section("portal.md › No such heading") == ""
    assert guide.search("review a draft card").startswith("[the portal's map")   # kelvin_help, as before


@pytest.fixture
def portal(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", None)
    settings = Settings(tmp_path / "settings.json")
    client = TestClient(create_app(rt, settings, ChatStore(tmp_path / "chats"), hosts=["testserver"], providers={}))
    return rt, settings, client, {"X-Kelvin-Token": settings.access_key}


def test_help_in_the_portal(portal):
    rt, settings, client, headers = portal
    with client:
        r = client.get("/api/help", params={"q": "change an instrument's limits"}, headers=headers).json()
        assert r["passages"] and all({"where", "text"} <= set(p) for p in r["passages"])
        assert any("Limits" in p["text"] for p in r["passages"])
        topics = client.get("/api/help", headers=headers).json()["topics"]
        assert any(t["where"] == "safety.md › The rules" for t in topics)
        assert "/use lockin" in client.get("/api/help", params={"section": "portal.md › Terminal"},
                                           headers=headers).json()["text"]
        assert client.get("/api/help", params={"section": "x.md › y"}, headers=headers).status_code == 404


def test_the_checklist_says_what_is_left_and_where_to_do_it(portal, tmp_path):
    rt, settings, client, headers = portal
    with client:
        r = client.get("/api/checklist", headers=headers).json()
        items = {i["key"]: i for i in r["items"]}
        assert list(items) == ["lab", "instruments", "limits", "pin", "phone", "data"]
        assert not items["lab"]["done"] and "virtual lab" in items["lab"]["detail"]
        assert not items["instruments"]["done"] and not items["limits"]["done"]
        assert not items["pin"]["done"] and "Lab owner's PIN" in items["pin"]["detail"]
        assert not items["phone"]["done"] and items["phone"]["action"] == "phone"
        assert items["data"]["done"] and str(rt.data_dir) in items["data"]["detail"]   # not the virtual lab's folder
        assert r["done"] == 1 and r["total"] == 6 and r["hidden"] is False

        # the lab as a person's own: instruments that answer, limits of its own, a PIN, a phone
        lab = rt.lab
        for name in ("smu", "magnet"):
            lab.instruments[name].backend.simulated = False
        for name in [n for n in lab.instruments if n not in ("smu", "magnet")]:
            lab.instruments.pop(name)
        rt.lab_info = dict(rt.lab_info, kind="file")
        lab.instruments["smu"].config.limits.clear()   # nobody set the lab's own: the card's, the maker's range
        items = {i["key"]: i for i in checklist(rt, settings, tmp_path)}
        assert items["lab"]["done"] and items["instruments"]["done"]
        assert not items["limits"]["done"] and "smu.V" in items["limits"]["detail"]   # the maker's full range
        for name in ("smu", "magnet"):
            inst = lab.instruments[name]
            for spec in inst.card.drives:
                if spec.numeric:
                    inst.config.limits[spec.name] = (0.0, 1.0)
        from labharness.instrument import Unavailable
        lab.instruments["magnet"].backend = Unavailable("magnet", "it did not answer")
        lab.instruments["smu"].card.status = "draft"
        items = {i["key"]: i for i in checklist(rt, settings, tmp_path)}
        assert items["limits"]["done"]
        assert not items["instruments"]["done"] and items["instruments"]["action"] == "cards"
        assert "magnet isn't connected" in items["instruments"]["detail"] and "smu's card is a draft" in \
            items["instruments"]["detail"]
        rt.owner.set("4321")
        settings.add_device("Ada's phone", "control", "hash")
        items = {i["key"]: i for i in checklist(rt, settings, tmp_path)}
        assert items["pin"]["done"] and items["phone"]["done"]

        r = client.post("/api/checklist", json={"hidden": True}, headers=headers).json()
        assert r["hidden"] is True and settings.public()["checklist"] is False   # Help keeps it
