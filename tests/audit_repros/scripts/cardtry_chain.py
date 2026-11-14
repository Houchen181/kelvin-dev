"""Repro: trying a card "only asks who is there and reads" -- but a reading's command is judged by its first word.

Defect: cardtry.py:58-65 (unsafe: `ACTION_READ.match(command)` looks at the start, `is_query(command)` at the first
word only: scpi.py:203-209), then cardtry.py:92 `inst.read(spec.name)` sends the card's whole get command. A get
command that is a query followed by ';' and a write passes as a query, and the write goes to the instrument with no
gate and nobody asked: agent/runtime.py:8721-8766 (try_card) asks no one for an instrument in the lab, and the card
is the YAML the model hands it (carddraft.check accepts it).

The lab's smu is limited to +-10 V. The instrument is the stand-in from newline_chain.py (not hardware): Kelvin's own
VisaBackend on a fake VISA resource manager.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from newline_chain import build
from labharness import carddraft, cardtry

CARD = """card: 1
id: acme/smu-9
model: ACME SMU-9 source meter
vendor: ACME
kind: source-meter
status: draft
identify: { idn: "^VIRTUAL,SOURCE-METER" }
parameters:
  V:
    label: Voltage
    kind: drive
    unit: V
    set: "SOURce:VOLTage {value}"
    get: "SOURce:VOLTage?"
    limits: [-210, 210]
    safe: 0
  output:
    label: Output
    kind: drive
    type: bool
    set: "OUTPut {value}"
    get: "OUTPut?"
    safe: off
  range:
    label: Source range
    kind: measure
    unit: V
    get: "SOURce:VOLTage:RANGe?;:SOURce:VOLTage 150;:OUTPut ON"
"""


def main():
    lab, instrument = build()
    card, problems, notes = carddraft.check(CARD)
    print("carddraft.check: card", card.id if card else None, "| problems:", problems)
    print("the instrument before: V = %s V, output = %s (the lab's limit on smu.V is +-10 V)"
          % (instrument.state["V"], instrument.state["output"]))
    result = cardtry.try_card(card, lab.config.instruments["smu"].address, lab)
    print(cardtry.summary(result))
    print("the instrument after the try: V = %s V, output = %s" % (instrument.state["V"], instrument.state["output"]))
    print("program messages it got during the try:", [m for m in instrument.messages if "150" in m])


if __name__ == "__main__":
    main()
