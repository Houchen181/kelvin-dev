"""The card findings of the 0.3.52 audit: I6 (mistyped keys accepted silently), I8 (the KDC101 card offered to every
Thorlabs instrument), I10 (the card checker reading YAML numbers by other rules than the lab) and I11
(card_versions.json missing released versions of cards Kelvin still brings)."""
import json
import re
import subprocess
from pathlib import Path

import pytest

from labharness import autocard, carddraft
from labharness.cards import CardError, parse_card
from labharness.collection import text_sha256
from labharness.discovery import match_card
from labharness.homelab import DATA, builtin_registry
from labharness.yamlio import load_yaml

ROOT = Path(__file__).resolve().parents[1]
CARD = """
card: 1
id: acme/sm-2000
model: ACME SM-2000 Source-Meter
kind: source-meter
status: draft
identify: { idn: "^ACME,SM-2000" }
parameters:
  V: { kind: drive, unit: V, set: "SOURce:VOLTage {value}", get: "SOURce:VOLTage?", limits: [-200, 200], safe: 0 }
  output: { kind: drive, type: bool, set: "OUTPut {value}", get: "OUTPut?", safe: off }
  I: { kind: measure, unit: A, get: ":READ?", read_when: output }
  T: { kind: measure, unit: K, get: "KRDG? A", plausible: [0.01, 1600], status: "RDGST? A" }
raw:
  blocked: ["CALibration"]
  reset: ["*RST"]
health:
  - { query: "*IDN?", expect: "ACME" }
display: { headline: I }
"""


# ------------------------------------------------------------------ I6: a mistyped key is refused

def test_a_cards_mistyped_keys_are_refused_with_what_they_may_have_meant():
    """I6: read_wen: lost "read only while the output is on", plausable: and statuss: the sensor-fault checks,
    raw.bloked: blocked nothing, and the card checker called the card fine."""
    card = parse_card(load_yaml(CARD), "spelled right")   # the same card, spelled right: every protection there
    assert card.params["I"].read_when == "output" and card.params["T"].plausible == (0.01, 1600.0)
    assert card.params["T"].status_cmd is not None and card.blocked and card.health and card.headline == "I"
    typos = {"read_when: output": "read_wen: output", "plausible:": "plausable:", 'status: "RDGST': 'statuss: "RDGST',
             "blocked:": "bloked:", "health:": "healt:", "headline:": "headlin:",
             'identify: { idn: "^ACME,SM-2000" }': 'identify: { idn: "^ACME,SM-2000", exmaple: "ACME,SM-2000" }'}
    wrong = CARD
    for right, typo in typos.items():
        wrong = wrong.replace(right, typo)
    with pytest.raises(CardError) as e:
        parse_card(load_yaml(wrong), "mistyped")
    said = e.value.errors
    for where, meant in (("parameters.I.read_wen", "read_when"), ("parameters.T.plausable", "plausible"),
                         ("parameters.T.statuss", "status"), ("raw.bloked", "blocked"), ("healt", "health"),
                         ("display.headlin", "headline"), ("identify.exmaple", "example")):
        assert any(s.startswith(where + ": ") and "did you mean %s?" % meant in s for s in said), (where, said)
    card, problems, _ = carddraft.check(wrong)   # Check, Save card and Mark reviewed say so too
    assert card is None and any("did you mean read_when?" in p for p in problems)
    wrong_health = CARD.replace('{ query: "*IDN?", expect: "ACME" }', '{ query: "*IDN?", expcet: "ACME" }')
    _, problems, _ = carddraft.check(wrong_health)
    assert any(p.startswith("health[0].expcet: ") and "did you mean expect?" in p for p in problems)
    fires = CARD + "actions:\n  fire: { send: \"*TRG\", label: Fire, drives: true }\n"
    assert parse_card(load_yaml(fires), "fires").actions["fire"].drives   # asks a person before it fires
    _, problems, _ = carddraft.check(fires.replace("drives: true", "drivs: true"))
    assert any(p.startswith("actions.fire.drivs: ") and "did you mean drives?" in p for p in problems)


def test_every_card_kelvin_brings_still_loads():
    assert len(list(builtin_registry())) >= 60


# ------------------------------------------------------------------ I8: the KDC101's card, for the KDC101

def test_the_kdc101_card_is_offered_to_the_kdc101_alone():
    """I8: its pattern ^Thorlabs offered the motor controller's card to every Thorlabs instrument a scan found."""
    registry = builtin_registry()
    kdc = registry.get("thorlabs/kdc101")
    own = "%s,%s,%s" % (kdc.vendor, kdc.model, "27000001")   # the identity Kelvin gives it (library.identity)
    for idn in (own, kdc.idn_example):
        assert re.search(kdc.idn_pattern, idn, re.I) and match_card(idn, registry).id == "thorlabs/kdc101", idn
    for idn in ("Thorlabs,PM16-120,1,1.0", "Thorlabs,MDT693B,1,1.0", "Thorlabs,KDC1010,1"):
        assert not re.search(kdc.idn_pattern, idn, re.I) and getattr(match_card(idn, registry), "id", None) != \
            "thorlabs/kdc101", idn
    assert match_card("Thorlabs,PM100D,P0012345,2.7.0", registry).id == "thorlabs/pm100"   # its own card still


# ------------------------------------------------------------------ I10: the checker reads numbers as the lab

NUMBERS = """
card: 1
id: acme/src-1
model: ACME source
kind: source
status: draft
parameters:
  I: { kind: drive, unit: A, set: "SOUR:CURR {value}", get: "SOUR:CURR?", limits: [-1e-3, 1e-3], safe: 0 }
  gain: { kind: measure, type: enum, set: "GAIN {value}", get: "GAIN?", values: [1e3, 1e4] }
"""


def test_the_card_checker_reads_numbers_as_the_lab_does():
    """I10: limits: [-1e-3, 1e-3] worked when the lab loaded the card (YAML 1.2 numbers), but Check, Save card and
    Mark reviewed (YAML 1.1: "1e-3" is text) refused it, and an enum's values had other names there."""
    lab = parse_card(load_yaml(NUMBERS), "as the lab loads it")
    card, problems, _ = carddraft.check(NUMBERS)
    assert problems == [] and card.params["I"].limits == lab.params["I"].limits == (-0.001, 0.001)
    assert card.params["gain"].values == lab.params["gain"].values
    drafted = autocard.make_safe("```yaml%s```" % NUMBERS, "ACME", "source", "ACME,SRC-1,1,1.0",
                                 "https://www.acme.example/m.pdf", "test:model")[0]   # what Kelvin's model wrote
    kept = parse_card(load_yaml(drafted), "kept")
    assert kept.params["I"].limits == (-0.001, 0.001) and kept.params["gain"].values == lab.params["gain"].values


# ------------------------------------------------------------------ I11: every released card version recorded

RELEASED = {("princeton/lightfield", "0.3.22"): "e6dd839cecabb2f056f4b638f30d77e0bfca7f82d852b8268607d23e92643ee3",
            ("princeton/lightfield", "0.3.23"): "3d35feb9e878a2a9bf8d9929c84b38160e1e65fa43586c0b2ed866d2d625ca9c",
            ("qick/rfsoc", "0.3.23"): "18cac1336d8dbee56c9e581cd09c28d9a943696400ee6e082417b28702fb6e8f"}


def test_released_versions_of_kelvins_cards_are_recorded():
    """I11: the versions of princeton/lightfield in 0.3.22 and 0.3.23, and of qick/rfsoc in 0.3.23, were missing, so a
    collection copy of one of them would have replaced the newer card this Kelvin brings."""
    listed = json.loads((DATA / "card_versions.json").read_text(encoding="utf-8"))["cards"]
    for (cid, version), digest in RELEASED.items():
        assert digest in listed[cid], (cid, version)
    for cid, digests in listed.items():   # the oldest first, as the file says: these before 0.3.24's
        assert len(digests) == len(set(digests)), cid
    assert listed["princeton/lightfield"][:2] == [RELEASED[("princeton/lightfield", v)] for v in ("0.3.22", "0.3.23")]


def test_those_versions_are_the_released_files():
    """The digests above, worked out again from the release commits themselves, where git has them."""
    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, timeout=60)
    log = git("log", "--format=%H %s", "--grep=^Kelvin 0\\.3\\.2[23]$")
    commits = {m.group(2): m.group(1) for m in re.finditer(r"(?m)^([0-9a-f]{40}) Kelvin (0\.3\.2[23])\s*$",
                                                            log.stdout.decode("utf-8", "replace"))}
    if log.returncode != 0 or set(commits) != {"0.3.22", "0.3.23"}:
        pytest.skip("this checkout has no release history (a shallow clone)")
    for (cid, version), digest in RELEASED.items():
        path = "src/labharness/data/cards/instruments/%s.yaml" % cid.replace("/", "_")
        blob = git("show", "%s:%s" % (commits[version], path))
        assert blob.returncode == 0 and text_sha256(blob.stdout) == digest, (cid, version)
