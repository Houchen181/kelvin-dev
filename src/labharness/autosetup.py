"""Kelvin sets up the instruments it finds: the first time it starts, and again whenever a person asks.

    scan       ask everything VISA and LabOne can see who it is. Read-only: nothing is set.
    lab.yaml   if the user has no lab yet, the instruments that matched a card are written as a lab config marked
               observe_only, and Kelvin connects to it. Readings and settings work at once; every drive stays at its
               safe value until a person reviews the limits in Lab setup and unlocks them.
    folders    each instrument's working folder (profiles.py): who it is, how Kelvin talks to it, every setting it
               has now, and its manual, which Kelvin reads before planning an involved scan.

A lab.yaml the user already has is never changed by this: instruments it does not list are only reported."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import profiles

LOCK = "observe_only: true   # Kelvin wrote this lab from what it found; review the limits, then unlock in Lab setup"
_LOCK_LINE = re.compile(r"^observe_only:.*(?:\r?\n|$)", re.M)


def draft_observed(found: List[Dict[str, Any]], registry: Any, lab_name: str = "My lab") -> str:
    """A lab config of the instruments a scan matched to cards, locked to observing until a person reviews it."""
    from .discovery import draft_from
    items, taken = [], []
    for f in found:
        if not f.get("card_id") or f.get("in_lab"):
            continue
        name = f.get("suggested") or "instr%d" % (len(items) + 1)
        while name in taken:
            name += "_2"
        taken.append(name)
        items.append({"name": name, "card": f["card_id"], "address": f["address"],
                      "note": (f.get("idn") or f.get("how") or "")[:60]})
    if not items:
        return ""
    text = draft_from(items, registry, lab_name)
    return text.replace("lab: %s\n" % lab_name, "lab: %s\n%s\n" % (lab_name, LOCK), 1)


def unlock(text: str) -> str:
    """The lab config with the observe_only line taken out (a person does this, after reviewing the limits)."""
    return _LOCK_LINE.sub("", text)


class AutoSetup:
    """Runs in the background so the portal opens at once. `status` says where it is, for the Lab setup panel."""

    def __init__(self, runtime: Any, settings: Any, home: Path, fetch: Optional[Callable[[str], bytes]] = None,
                 scan: Optional[Callable[[], List[Dict[str, Any]]]] = None, can_switch: Callable[[], bool] = lambda: True):
        self.runtime, self.settings, self.home = runtime, settings, Path(home)
        self.fetch = fetch or profiles.http_get
        self.scan = scan or runtime.scan_found
        self.can_switch = can_switch
        self.status: Dict[str, Any] = {"state": "idle"}
        self._thread: Optional[threading.Thread] = None

    @property
    def lab_file(self) -> Path:
        return self.home / "lab.yaml"

    def start(self, reason: str = "start") -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._thread = threading.Thread(target=self.run, args=(reason,), name="kelvin-autosetup", daemon=True)
        self._thread.start()
        return True

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _set(self, state: str, **more: Any) -> None:
        self.status = dict(more, state=state, at=time.time())
        self.runtime.setup_status = self.status

    def run(self, reason: str = "start") -> Dict[str, Any]:
        rt = self.runtime
        manuals = self.settings.get("manuals") != "off"
        found_by_address: Dict[str, Dict[str, Any]] = {}
        try:
            user_lab = rt.lab_info.get("kind") == "file"
            if not user_lab and not self.lab_file.exists() and self.settings.get("lab") != "virtual":
                self._set("scanning", message="Looking for connected instruments (asking each who it is)")
                try:
                    found = self.scan()
                except Exception as e:   # no VISA library: nothing to set up
                    self._set("done", message="No instruments looked for: %s" % profiles._first_line(e), found=0)
                    return self.status
                found_by_address = {f["address"]: f for f in found}
                matched = [f for f in found if f.get("card_id")]
                unknown = [f for f in found if f.get("idn") and not f.get("card_id")]
                if not matched:
                    self._set("done", found=0, unknown=len(unknown), message=(
                        "%d instrument%s answered but Kelvin has no card for %s yet; draft one in Lab setup"
                        % (len(unknown), "" if len(unknown) == 1 else "s", "it" if len(unknown) == 1 else "them")
                        if unknown else "No instruments found"))
                    return self.status
                if not self._connect(matched):
                    return self.status
                user_lab = True
            if not user_lab:
                self._set("done", message="Using the virtual lab; your own instruments are set up in Lab setup")
                return self.status
            names = sorted(n for n, i in rt.lab.instruments.items() if not i.simulated)
            self._set("reading", message="Reading the settings of %s" % ", ".join(names),
                      instruments=names, observe_only=rt.lab.config.observe_only)
            report = profiles.prepare(self.home, rt.lab, rt.lock, found_by_address,
                                      manuals=manuals, fetch=self.fetch)
            fetched = [n for n, r in report.items() if "pages" in str(r.get("manual", ""))]
            missing = [n for n, r in report.items() if str(r.get("manual", "")).startswith("not fetched")]
            self._set("done", instruments=names, report=report, observe_only=rt.lab.config.observe_only,
                      message="%d instrument%s set up%s%s" % (
                          len(names), "" if len(names) == 1 else "s",
                          "; manuals for %s" % ", ".join(fetched) if fetched else "",
                          "; no manual yet for %s" % ", ".join(missing) if missing else ""))
            return self.status
        except Exception as e:
            self._set("failed", message="Setting up instruments failed: %s" % profiles._first_line(e))
            return self.status

    def _connect(self, matched: List[Dict[str, Any]]) -> bool:
        """Write the observed lab and connect to it. False (and a status saying why) if that is not possible now."""
        from .virtual_lab import build_lab, lab_registry
        rt = self.runtime
        registry = lab_registry(self.home)
        text = draft_observed(matched, registry)
        names = ", ".join("%s (%s)" % (f.get("suggested"), f.get("model")) for f in matched)
        self._set("connecting", message="Found %s; connecting" % names, found=len(matched))
        tmp = self.lab_file.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.lab_file)
        self.settings.update({"lab": "mine"})
        if not self.can_switch():
            self._set("done", found=len(matched), message="Found %s and wrote lab.yaml; it opens when Kelvin is idle "
                      "(or press Use my lab in Lab setup)" % names)
            return False
        try:
            lab = build_lab(self.lab_file, None, 0, registry)
        except Exception as e:
            self._set("done", found=len(matched), message="Found %s but could not connect: %s"
                      % (names, profiles._first_line(e)))
            return False
        info = {"kind": "file", "path": str(self.lab_file), "error": None, "has_file": True, "auto": True}
        try:
            rt.switch_lab(lab, None, info, "Kelvin auto-setup")
        except RuntimeError as e:
            lab.close()
            self._set("done", found=len(matched), message="Found %s; %s" % (names, e))
            return False
        rt.notifier("setup", "Kelvin set up %d instrument%s" % (len(matched), "" if len(matched) == 1 else "s"),
                    names + ". Readings and settings work now; drives stay at safe until you review the limits in "
                    "Lab setup and unlock them.")
        return True
