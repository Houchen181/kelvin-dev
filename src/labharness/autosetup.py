"""Kelvin sets up the instruments it finds: the first time it starts, and again whenever a person asks.

    scan       ask everything VISA and LabOne can see who it is. Read-only: nothing is set.
    lab.yaml   if the user has no lab yet, the instruments that matched a card are written as a lab config, each
               marked added_by: kelvin, and Kelvin connects to it. Readings and settings work at once; Kelvin asks a
               person before every output change on them until they review the limits in Lab setup.
    folders    each instrument's working folder (profiles.py): who it is, how Kelvin talks to it, every setting it
               has now, and its manual, which Kelvin reads before planning an involved scan.

A lab.yaml the user already has is not changed by this: instruments it does not list are only reported (Kelvin adds
one when someone asks for it: runtime.add_instrument)."""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import profiles

log = logging.getLogger("kelvin.setup")   # the desktop app writes this to ~/Kelvin/kelvin.log
_LOCK_LINE = re.compile(r"^observe_only:.*(?:\r?\n|$)", re.M)   # what older versions wrote for a whole lab
_ADDED_LINE = re.compile(r"^[ \t]+added_by:[ \t]*kelvin\b.*(?:\r?\n|$)", re.M)


def draft_found(found: List[Dict[str, Any]], registry: Any, lab_name: str = "My lab") -> str:
    """A lab config of the instruments a scan matched to cards, each marked as added by Kelvin. One the lab's own
    scripts drive (from_scripts, a scripts.Instrument) gets the values they use as its limits; one only they
    identified (the scan couldn't), the connection settings they use."""
    from .discovery import draft_from
    from .scripts import limits as script_limits
    items, taken = [], []
    for f in found:
        if not f.get("card_id") or f.get("in_lab"):
            continue
        inst = f.get("from_scripts")
        name = f.get("suggested") or "instr%d" % (len(items) + 1)
        while name in taken:
            name += "_2"
        taken.append(name)
        item = {"name": name, "card": f["card_id"], "address": f["address"], "added_by": "kelvin",
                "note": (f.get("idn") or f.get("how") or "")[:60]}
        if inst is not None:
            item["limits"] = {p: list(v) for p, v in script_limits(registry.get(f["card_id"]), inst.commands).items()}
            if f.get("connection"):
                item["connection"] = f["connection"]
        items.append(item)
    if not items:
        return ""
    return draft_from(items, registry, lab_name)


def reviewed(text: str) -> str:
    """The lab config with Kelvin's marks taken out, every added_by: kelvin (and an older observe_only line): a
    person does this after reviewing the limits."""
    return _ADDED_LINE.sub("", _LOCK_LINE.sub("", text))


class AutoSetup:
    """Runs in the background so the portal opens at once. `status` says where it is, for the Lab setup panel."""

    def __init__(self, runtime: Any, settings: Any, home: Path, fetch: Optional[Callable[[str], bytes]] = None,
                 scan: Optional[Callable[[], List[Dict[str, Any]]]] = None, can_switch: Callable[[], bool] = lambda: True,
                 make_card: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None):
        self.runtime, self.settings, self.home = runtime, settings, Path(home)
        self.fetch = fetch or profiles.http_get
        self.scan = scan or runtime.scan_found
        self.can_switch = can_switch
        self.make_card = make_card          # autocard.make_card with the app's model, for instruments with no card
        self._manuals: Dict[str, Dict[str, Any]] = {}   # address -> the manual found while writing its card
        self._written: List[str] = []                    # models Kelvin wrote cards for in this run
        self._card_problems: List[str] = []              # why a card could not be written
        self.status: Dict[str, Any] = {"state": "idle"}
        self._thread: Optional[threading.Thread] = None
        self.first = False   # setting up a lab that has no lab config yet: the portal asks for the lab's scripts
        self.scripts: Any = None   # the lab's scripts, read (scripts.Survey), once a person named their folder
        self._drafted = False      # the first lab config is written: the scripts come too late to shape it
        self._scripts_lock = threading.Lock()

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
        self.status = dict(more, state=state, at=time.time(), first=self.first)
        self.runtime.setup_status = self.status

    def use_scripts(self, folder: str) -> Dict[str, Any]:
        """The lab's own scripts, which a person pointed Kelvin to: read now (never run; files only, so not waiting
        for the instruments) for the first lab config, if it isn't written yet. now: they shape it; otherwise
        Set up's From your scripts adds what they show. Raises OSError for a folder that isn't there."""
        from .scripts import survey
        found = survey(folder, self.runtime.script_cards())
        with self._scripts_lock:
            self.scripts = found
            now = self.first and not self._drafted and self._thread is not None and self._thread.is_alive()
        log.info("The lab's scripts in %s: %d instrument(s) in %d script(s)%s", folder, len(found.instruments),
                 len(found.scripts), "; used for the first lab config" if now else "")
        return {"folder": str(folder), "instruments": len(found.instruments), "scripts": len(found.scripts),
                "now": now}

    def _with_scripts(self, matched: List[Dict[str, Any]], found: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """What the scan matched, with what the lab's scripts say of each (the name, the values used), and the
        instruments the scan saw that said nothing to who-are-you (serial devices often don't), by the card the
        scripts surely fit, with their settings. One that answered with a reply no card matches gets its card
        written from its manual, as without scripts: its own reply outweighs the scripts' guess."""
        from .adapters import resource_name
        from .discovery import Found
        from .scripts import connection
        with self._scripts_lock:
            self._drafted = True
            known = self.scripts
        if known is None:
            return matched
        by = {resource_name(i.address): i for i in known.instruments if i.address}
        out = [dict(f, from_scripts=by[resource_name(f["address"])]) if resource_name(f["address"]) in by else f
               for f in matched]
        have = {resource_name(f["address"]) for f in out}
        seen = {resource_name(f["address"]): f for f in found if not f.get("in_lab") and not f.get("idn")}
        for key, inst in by.items():
            card = inst.card or {}
            if key in have or key not in seen or not card.get("sure"):
                continue
            out.append(dict(seen[key], card_id=card["id"], model=card["model"], kind=card["kind"], from_scripts=inst,
                            how="your scripts: " + str(card.get("why") or "")[:50],
                            connection=connection(inst.settings)))
        taken = {f.get("suggested") for f in out if f.get("from_scripts") is None}
        for f in out:   # what the lab calls it in its scripts
            inst = f.get("from_scripts")
            if inst is not None and inst.name.isidentifier() and inst.name not in taken:
                f["suggested"] = inst.name
            elif inst is not None and not f.get("suggested"):
                f["suggested"] = Found(f["address"], kind=f["kind"]).suggest_name(sorted(n for n in taken if n))
            taken.add(f.get("suggested"))
        return out

    def _done(self, message: str, **more: Any) -> Dict[str, Any]:
        """Finish, saying why (the Lab panel shows it while Kelvin is on the virtual lab), and log it."""
        looked = self.runtime.last_survey if more.get("lines") is not None else None
        if looked:
            for line in looked.get("lines") or []:
                log.info("  %s", line)
        log.info("Instrument setup: %s", message)
        self._set("done", message=message, **more)
        return self.status

    def run(self, reason: str = "start") -> Dict[str, Any]:
        rt = self.runtime
        manuals = self.settings.get("manuals") != "off"
        found_by_address: Dict[str, Dict[str, Any]] = {}
        try:
            if rt.lab.connecting:   # Kelvin opened before connecting the person's lab, which can take minutes
                self._connect_pending()
            if reason == "connect":   # only that: setting up instruments is off for this Kelvin
                return self._done(self.status.get("message") or "Connected")
            user_lab = rt.lab_info.get("kind") == "file"
            if reason == "start" and self.settings.get("auto_setup") == "off":
                return self._done("Looking for instruments when Kelvin starts is switched off in Settings")
            if not user_lab and not self.lab_file.exists() and self.settings.get("lab") != "virtual":
                self.first = True
                self._set("scanning", message="Looking for connected instruments (asking each who it is)")
                log.info("Instrument scan (%s): looking for connected instruments", reason)
                try:
                    found = self.scan()
                except Exception as e:   # no VISA library: nothing to set up
                    return self._done("Kelvin could not look for instruments: %s" % profiles._first_line(e), found=0)
                looked = rt.last_survey or {}
                found_by_address = {f["address"]: f for f in found}
                matched = [f for f in found if f.get("card_id") and not f.get("in_lab")]
                unknown = [f for f in found if f.get("idn") and not f.get("card_id") and not f.get("in_lab")]
                written, card_problems = self._write_cards(unknown, matched)
                matched = self._with_scripts(matched + written, found)
                unknown = [f for f in unknown if f["address"] not in {w["address"] for w in matched}]
                for f in found:
                    log.info("  %s: %s", f["address"], f.get("idn") or f.get("how") or "no answer")
                if not matched and card_problems:
                    return self._done(" ".join(card_problems), found=0, unknown=len(unknown),
                                      lines=looked.get("lines") or [])
                if not matched:
                    named = [f["model"] for f in unknown if f.get("collection") and f.get("model")]
                    return self._done(looked.get("reason") or (
                        "%d instrument%s answered but Kelvin has no card for %s yet%s; make one in Lab setup"
                        % (len(unknown), "" if len(unknown) == 1 else "s", "it" if len(unknown) == 1 else "them",
                           " (%s)" % ", ".join(named) if named else "")
                        if unknown else "No instruments found on this computer"),
                        found=0, unknown=len(unknown), lines=looked.get("lines") or [])
                if not self._connect(matched):
                    return self.status
                user_lab = True
            if not user_lab:
                why = rt.lab_info.get("error") or ("You chose the virtual lab in Lab setup" if self.lab_file.exists()
                                                    or self.settings.get("lab") == "virtual" else "")
                return self._done(why or "Using the virtual lab")
            names = sorted(n for n, i in rt.lab.instruments.items() if not i.simulated and i.enabled)
            self._set("reading", message="Reading the settings of %s" % ", ".join(names),
                      instruments=names, unreviewed=rt.lab.config.unreviewed())
            for name, ic in rt.lab.config.instruments.items():   # manuals found while writing cards: no second download
                m = self._manuals.pop(ic.address, None)
                if m and name in rt.lab.instruments:
                    try:
                        profiles.store_manual(self.home, name, rt.lab.instruments[name].card, m["data"], m["url"])
                    except Exception as e:
                        log.info("  could not keep the %s manual: %s", name, profiles._first_line(e))
            report = profiles.prepare(self.home, rt.lab, rt.lock, found_by_address,
                                      manuals=manuals, fetch=self.fetch)
            fetched = [n for n, r in report.items() if "pages" in str(r.get("manual", ""))]
            missing = [n for n, r in report.items() if str(r.get("manual", "")).startswith("not fetched")]
            log.info("Instrument setup: %s; %s", ", ".join(names), report)
            self._set("done", instruments=names, report=report, unreviewed=rt.lab.config.unreviewed(),
                      written=list(self._written),
                      message="%d instrument%s set up%s%s%s%s" % (
                          len(names), "" if len(names) == 1 else "s",
                          "; " + " ".join(self._card_problems).rstrip(".") if self._card_problems else "",
                          "; Kelvin wrote draft cards for %s: review them in Lab setup" % ", ".join(self._written)
                          if self._written else "",
                          "; manuals for %s" % ", ".join(fetched) if fetched else "",
                          "; no manual yet for %s" % ", ".join(missing) if missing else ""))
            return self.status
        except Exception as e:
            log.exception("Instrument setup failed")
            self._set("failed", message="Setting up instruments failed: %s" % profiles._first_line(e))
            return self.status

    def _connect_pending(self) -> None:
        """Connect the person's lab, one instrument after another, saying which (the portal shows it)."""
        rt = self.runtime

        def progress(name: str, i: int, n: int) -> None:
            inst = rt.lab.instruments.get(name)
            model = " (%s)" % inst.card.model.split(" (")[0] if inst is not None else ""
            self._set("connecting", message="Connecting %s%s, %d of %d" % (name, model, i + 1, n), step=i + 1,
                      steps=n)
        result = rt.connect_pending(progress)
        missing = result["not_connected"]
        message = ("Connected %s" % ", ".join(result["connected"]) if result["connected"] else "No instrument connected")
        if missing:
            message += "; not connected: " + "; ".join("%s (%s)" % (n, profiles._first_line(why))
                                                       for n, why in missing.items())
        log.info("Instruments: %s", message)
        self._set("connected", message=message, connected=result["connected"], not_connected=sorted(missing))

    def _write_cards(self, unknown: List[Dict[str, Any]], matched: List[Dict[str, Any]]) -> Any:
        """For instruments that answered but match no card: find the manual and write a draft card (autocard.py)."""
        if not unknown or self.make_card is None or self.settings.get("auto_cards") == "off":
            return [], []
        from .autocard import maker_model
        from .discovery import Found
        written, problems = [], self._card_problems
        taken = [f.get("suggested") for f in matched if f.get("suggested")]
        for f in unknown:
            maker, model = maker_model(str(f.get("idn") or ""))
            name = f.get("model") if f.get("collection") else ""   # the instrument collection's name for it
            self._set("drafting", message="Finding the %s manual and writing its card (a minute or two)"
                      % (name or "%s %s" % (maker or "unknown", model or "instrument")))
            log.info("  writing a card for %s at %s", f.get("idn"), f["address"])
            try:
                r = self.make_card(f)
            except Exception as e:
                r = {"error": "Kelvin could not write the card for %s: %s" % (f["address"], profiles._first_line(e))}
            if r.get("error"):
                problems.append(r["error"] + ".")
                log.info("  %s", r["error"])
                continue
            name = Found(f["address"], kind=r["kind"]).suggest_name(taken)
            taken.append(name)
            self._written.append(r["model"])
            written.append(dict(f, card_id=r["card_id"], model=r["model"], kind=r["kind"], suggested=name,
                                how="card written by Kelvin"))
            if r.get("manual"):
                self._manuals[f["address"]] = r["manual"]
            self.runtime.notifier("setup", "Kelvin wrote a card for the %s" % r["model"],
                                  "From its manual (%s). It is a draft: Kelvin asks you before each setting or output "
                                  "change on it until you review it in Lab setup.%s" % (
                                      r["manual"]["url"], " Left out or unsure: " + " ".join(r["notes"])
                                      if r.get("notes") else ""))
        return written, problems

    def _connect(self, matched: List[Dict[str, Any]]) -> bool:
        """Write the observed lab and connect to it. False (and a status saying why) if that is not possible now."""
        from .virtual_lab import build_lab, lab_registry
        rt = self.runtime
        registry = lab_registry(self.home, rt.collection)
        text = draft_found(matched, registry)
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
            lab = build_lab(self.lab_file, None, 0, registry, getattr(self.settings, "switched_off", None))
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
                    names + ". Readings and settings work now; Kelvin asks you before each output change until you "
                    "review their limits in Lab setup.")
        return True
