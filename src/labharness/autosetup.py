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


def _write_new(path: Path, text: str) -> bool:
    """Write a new file whole, never over one that is there (a person's own lab config, saved while the set-up
    scanned): False when one is. Written beside it, then linked into place, so a reader never sees half of it."""
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        try:
            os.link(str(tmp), str(path))   # fails if the file is there, on every system
        except FileExistsError:
            return False
        except OSError:   # no links on this drive (FAT, some network folders): made only if it isn't there
            try:
                with open(str(path), "x", encoding="utf-8") as f:
                    f.write(text)
            except FileExistsError:
                return False
        return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


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
        self.racks: Dict[str, Any] = {}   # their Special Measure rack, by address: (its instrument, the rack in words)
        self._drafted = False      # the first lab config is written: the scripts come too late to shape it
        self._scripts_lock = threading.Lock()
        self._running = False      # a set-up runs (start): another start waits for it (lab changed: runs after it)
        self._again = False
        self._missing: Dict[str, str] = {}

    @property
    def lab_file(self) -> Path:
        return self.home / "lab.yaml"

    def start(self, reason: str = "start") -> bool:
        """The set-up, in the background. One asked for after a change of the lab while one runs (Save and connect
        during the start's set-up) runs once that one ends: its instruments once got no folder, settings or manual
        until the next start."""
        with self._scripts_lock:
            if self._running:
                if reason == "lab changed":
                    self._again = True
                return False
            self._running, self._again = True, False
            self._thread = threading.Thread(target=self._runs, args=(reason,), name="kelvin-autosetup", daemon=True)
            self._thread.start()
        return True

    def _runs(self, reason: str) -> None:
        try:
            self.run(reason)
            while True:   # a change of the lab while it ran: what is new set up too
                with self._scripts_lock:
                    if not self._again:
                        self._running = False   # with the lock: a start from now on starts anew
                        return
                    self._again = False
                self.run("lab changed")
        finally:
            with self._scripts_lock:
                self._running = False

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _set(self, state: str, **more: Any) -> None:
        self.status = dict(more, state=state, at=time.time(), first=self.first)
        self.runtime.setup_status = self.status

    def use_scripts(self, folder: Any) -> Dict[str, Any]:
        """The lab's own scripts, which a person pointed Kelvin to (folder: a folder, or the lab's scripts folders, a
        list read as one): read now (never run; files only, so not waiting for the instruments) for the first lab
        config, if it isn't written yet. now: they shape it; otherwise Set up's Scripts… adds what they show. A folder
        with Special Measure's setup scripts is read as its rack (specialmeasure.py), which the script survey can't
        read (field test #9: 0 instruments in 14 scripts): the instruments its newest setup loads, named as the rack
        names them, at the addresses its numbers, flags, smloadinst, gpib and visa calls give; what the rack proposes
        as their limits waits in each one's review (_keep_proposed). Raises OSError for a folder that isn't there."""
        import dataclasses
        from . import specialmeasure
        from .scripts import Script, Survey, survey
        registry = self.runtime.script_cards()
        paths = list(folder) if isinstance(folder, (list, tuple)) else [folder]
        racks, plain = [], []
        for p in paths:
            try:
                sm = specialmeasure.survey(p, registry, scans=False)
            except (OSError, ValueError):   # not there, or in Kelvin's own folder: the script survey says so
                sm = None
            if sm is not None and sm.setup is not None:
                racks.append(sm)
            else:
                plain.append(p)
        found = Survey("")
        try:
            if plain:
                found = survey(plain if isinstance(folder, (list, tuple)) else plain[0], registry)
        except (OSError, ValueError) as e:   # with a rack read, another folder that can't be read is only said
            if not racks:
                raise
            found.skipped.append(str(e))
        rack: Dict[str, Any] = {}
        for sm in racks:
            for ri in specialmeasure._hardware(sm.setup):
                if ri.used and ri.inst.address:   # what the setup loads (its *_Use flag on), the first at an address
                    rack.setdefault(specialmeasure.rack_key(ri.inst.address),
                                    (ri, "your Special Measure rack (%s in %s)" % (sm.setup.path, sm.folder)))
        if racks:   # a start-up command is no value the lab drives it at: the rack proposes limits, Kelvin sets none
            found.instruments = [i for i in found.instruments if specialmeasure.rack_key(i.address) not in rack] + \
                [dataclasses.replace(ri.inst, commands=[]) for ri, _ in rack.values()]
            found.scripts += [Script(sm.setup.path, "matlab") for sm in racks]
            found.folders = found.folders + [sm.folder for sm in racks]
            found.folder = found.folders[0] if len(found.folders) == 1 else \
                ", ".join(found.folders[:-1]) + " and " + found.folders[-1]
        with self._scripts_lock:
            self.scripts, self.racks = found, rack
            now = self.first and not self._drafted and self._thread is not None and self._thread.is_alive()
        log.info("The lab's scripts in %s: %d instrument(s) in %d script(s)%s%s", found.folder, len(found.instruments),
                 len(found.scripts), "; Special Measure's rack in %s" % ", ".join(
                     "%s (%s)" % (sm.folder, sm.setup.path) for sm in racks) if racks else "",
                 "; used for the first lab config" if now else "")
        return {"folder": found.folder, "folders": found.folders, "instruments": len(found.instruments),
                "scripts": len(found.scripts), "now": now}

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

    def _scan(self) -> List[Dict[str, Any]]:
        """The scan, saying as it goes which port or address it asks now (a port that takes long shows which)."""
        import inspect

        def asking(what: str) -> None:
            self._set("scanning", message="Looking for connected instruments: %s" % what)
        try:
            takes = "progress" in inspect.signature(self.scan).parameters
        except (TypeError, ValueError):
            takes = False
        return self.scan(progress=asking) if takes else self.scan()

    def _done(self, message: str, **more: Any) -> Dict[str, Any]:
        """Finish, saying why (the Lab panel shows it while the lab has no instruments), and log it."""
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
        self._missing: Dict[str, str] = {}   # what didn't connect, and why: said with the set-up's last words
        started = rt.lab   # a person's own lab coming in meanwhile is never replaced by the set-up's draft
        try:
            if rt.lab.connecting:   # Kelvin opened before connecting the person's lab, which can take minutes
                self._connect_pending()
            if reason == "connect":   # only that: setting up instruments is off for this Kelvin
                return self._done(self.status.get("message") or "Connected")
            user_lab = rt.lab_info.get("kind") == "file"
            if reason == "start" and self.settings.get("auto_setup") == "off":
                return self._done("Looking for instruments when Kelvin starts is switched off in Settings")
            if not user_lab and not self.lab_file.exists():
                self.first = True
                self._set("scanning", message="Looking for connected instruments (asking each who it is)")
                log.info("Instrument scan (%s): looking for connected instruments", reason)
                try:
                    found = self._scan()
                except Exception as e:   # no VISA library: nothing to set up
                    return self._done("Kelvin could not look for instruments: %s" % profiles._first_line(e), found=0)
                looked = rt.last_survey or {}
                found_by_address = {f["address"]: f for f in found}
                matched = [f for f in found if f.get("card_id") and not f.get("in_lab")]
                unknown = [f for f in found if f.get("idn") and not f.get("card_id") and not f.get("in_lab")]
                if matched and unknown and self.make_card is not None and self.settings.get("auto_cards") != "off":
                    # the ones Kelvin knows connect first: writing the others' cards takes minutes of model calls
                    # (5.7), and each comes in as it is written (add_instrument), the ones connected meanwhile untouched
                    first = self._with_scripts(matched, found)
                    for f in found:
                        log.info("  %s: %s", f["address"], f.get("idn") or f.get("how") or "no answer")
                    if not self._connect(first, started):
                        return self.status
                    user_lab = True
                    taken = {w["address"] for w in first}
                    written, _ = self._write_cards([f for f in unknown if f["address"] not in taken], first)
                    self._add_written(written)
                    matched, unknown = [], []
                else:
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
                    if not self._connect(matched, started):
                        return self.status
                    user_lab = True
            if not user_lab:
                return self._done(rt.lab_info.get("error") or "No instruments set up yet")
            swaps = self._check_identities()   # another instrument of the same model at an instrument's address (6.2)
            names = sorted(n for n, i in rt.lab.instruments.items() if not i.simulated and i.enabled)
            # after a change of the lab (Save and connect, an instrument added), only what is new: the rest have their
            # folders, and each one read again cost a minute of timeouts on a lab with one silent instrument
            reading = names if reason != "lab changed" else [n for n in profiles.unprepared(self.home, rt.lab)
                                                              if n in names]
            self._set("reading", message="Reading the settings of %s" % (", ".join(reading) or "nothing new"),
                      instruments=names, unreviewed=rt.lab.config.unreviewed())
            for name, ic in list(rt.lab.config.instruments.items()):   # manuals found while writing cards: no second download
                m = self._manuals.pop(ic.address, None)
                if m and name in rt.lab.instruments:
                    try:
                        profiles.store_manual(self.home, name, rt.lab.instruments[name].card, m["data"], m["url"])
                    except Exception as e:
                        log.info("  could not keep the %s manual: %s", name, profiles._first_line(e))
            def busy(text: Optional[str]) -> None:   # the Lab panel's busy line says which instrument is read now
                try:
                    rt.busy = text
                except Exception:
                    pass
            report = profiles.prepare(self.home, rt.lab, rt.lock, found_by_address, manuals=manuals,
                                      fetch=self.fetch, names=reading, busy=busy)
            profiles.hold_serials(self.home, rt.lab)   # the serial numbers kept now (the first set-up's, just written)
            kept_aside = self._ask_swapped(swaps)
            fetched = [n for n, r in report.items() if "pages" in str(r.get("manual", ""))]
            missing = [n for n, r in report.items() if str(r.get("manual", "")).startswith("not fetched")]
            log.info("Instrument setup: %s; %s", ", ".join(names), report)
            away = rt.lab.unavailable   # still not connected, with why: said, not left out
            unconnected = {n: away[n] for n in self._missing if n in away}
            self._set("done", instruments=names, report=report, unreviewed=rt.lab.config.unreviewed(),
                      written=list(self._written), not_connected=sorted(unconnected),
                      message="%d instrument%s set up%s%s%s%s%s" % (
                          len(names), "" if len(names) == 1 else "s",
                          "; not connected: " + "; ".join("%s (%s)" % (n, profiles._first_line(why))
                                                          for n, why in sorted(unconnected.items()))
                          if unconnected else "",
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

    def _add_written(self, written: List[Dict[str, Any]]) -> None:
        """Instruments whose cards were written after the known ones connected: each added to the lab as it comes
        (add_instrument: its entry, then it alone connected; the others stay as they are). One that can't be added is
        said with the set-up's last words."""
        rt = self.runtime
        for w in written:
            self._set("connecting", message="Adding %s (%s), whose card Kelvin wrote" % (w.get("suggested"),
                                                                                        w.get("model")))
            try:
                said = rt.add_instrument(w["address"], card=w["card_id"], name=w.get("suggested") or "", by="setup")
            except Exception as e:
                said = "not added: %s" % profiles._first_line(e)
            log.info("  %s: %s", w["address"], said.splitlines()[0] if said else "")
            if not said.startswith("added"):
                self._card_problems.append("%s: %s." % (w.get("suggested") or w["address"], said.rstrip(".")))

    def _check_identities(self) -> Dict[str, Any]:
        """Each instrument with a kept serial number asked who it is (one query, the instrument held for it, never
        waiting out a run): one that answers with another serial number of the same model is set aside (Lab.
        check_identity). Swapped USB-serial cables or DHCP addresses once had Kelvin drive the other cryostat's
        controller. {name: (why, what it answered)}."""
        rt = self.runtime
        profiles.hold_serials(self.home, rt.lab)
        out: Dict[str, Any] = {}
        for name, inst in list(rt.lab.instruments.items()):
            if inst.simulated or not inst.enabled or not getattr(inst, "kept_serial", None):
                continue
            if not profiles._take(rt.lock, name, 10.0):   # other work has it: asked at the next set-up
                continue
            try:
                rt.lab.check_identity([name])
            finally:
                rt.lock.release()
        away = rt.lab.unavailable   # the swapped ones, set aside: now, or by a health check before (at start, a run)
        return {n: v for n, v in dict(getattr(rt.lab, "swaps", {})).items() if n in away}

    def _ask_swapped(self, swaps: Dict[str, Any]) -> List[str]:
        """A person says, for each instrument set aside as another one of the same model, whether the one that answers
        now is the one to use (a replacement, one back from repair): yes keeps its serial number and connects it again;
        no, or no answer, leaves it set aside. Never auto mode's to answer. The ones left aside."""
        from .gate import ApprovalRequest
        from .profiles import parse_idn
        rt = self.runtime
        aside = []
        for name, (why, idn) in swaps.items():
            inst = rt.lab.instruments.get(name)
            if inst is None:
                continue
            serial = parse_idn(idn).get("serial", "")
            rt.notifier("setup", "%s set aside: another instrument of its model answers there" % name, why)
            self._set("asking", message="Is the instrument at %s your %s? It answers with serial number %s"
                      % (inst.config.address, name, serial))
            request = ApprovalRequest("Use the instrument at %s as %s?" % (inst.config.address, name), [
                why, "Yes, if it is the one to use as %s now (a replacement, or back from repair): Kelvin keeps serial "
                     "number %s for it and connects it again. No: it stays set aside." % (name, serial)])
            try:
                yes = bool(rt.approver.approve(request))
            except Exception:
                yes = False
            if not yes:
                aside.append(name)
                self._missing[name] = why
                continue
            profiles.accept_serial(self.home, name, serial, inst.card.id, inst.config.address)
            profiles.hold_serials(self.home, rt.lab)
            getattr(rt.lab, "swaps", {}).pop(name, None)
            try:
                rt.lab.reconnect(name)
            except Exception as e:   # set aside again with why: said
                self._missing[name] = profiles._first_line(e)
                aside.append(name)
        return aside

    def _connect_pending(self) -> None:
        """Connect the person's lab, one instrument after another, saying which (the portal shows it)."""
        rt = self.runtime

        def progress(name: str, i: int, n: int, waiting: str = "") -> None:
            inst = rt.lab.instruments.get(name)
            model = " (%s)" % inst.card.model.split(" (")[0] if inst is not None else ""
            self._set("connecting", message="Connecting %s%s, %d of %d%s" % (
                name, model, i + 1, n, ": waiting for %s, which has the lab" % waiting if waiting else ""),
                step=i + 1, steps=n)
        result = rt.connect_pending(progress)
        missing = result["not_connected"]
        self._missing = dict(getattr(self, "_missing", {}), **missing)   # said again with the set-up's last words
        message = ("Connected %s" % ", ".join(result["connected"]) if result["connected"] else "No instrument connected")
        if missing:
            message += "; not connected: " + "; ".join("%s (%s)" % (n, profiles._first_line(why))
                                                       for n, why in missing.items())
        log.info("Instruments: %s", message)
        self._set("connected", message=message, connected=result["connected"], not_connected=sorted(missing))

    def _from_collection(self, unknown: List[Dict[str, Any]], matched: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The instruments that answered but matched no card, with the card the instrument collection has for them
        once it is up to date. At a first start Kelvin may not have its copy yet (it is fetched in the background), and
        the card writer then fell back to guessing manuals on the web, never trying the reference manual the
        collection links (field test l08). A sync that fails says so in the portal (collection.notify)."""
        coll = getattr(self.runtime, "collection", None)
        if coll is None or not unknown:
            return []
        from .discovery import Found, match_card
        from .homelab import lab_registry
        self._set("drafting", message="Bringing Kelvin's instrument collection up to date first: its cards, and where "
                                      "the makers publish their manuals")
        try:
            status = coll.sync()   # a problem goes into its status, not raised
        except Exception as e:
            status = {"error": profiles._first_line(e)}
        if status.get("error"):
            log.info("  instrument collection: %s", status["error"])
        registry = lab_registry(self.home, coll)
        taken = [f.get("suggested") for f in matched if f.get("suggested")]
        out = []
        for f in unknown:
            card = match_card(str(f.get("idn") or ""), registry)
            if card is None:
                continue
            name = Found(f["address"], kind=card.kind).suggest_name(taken)
            taken.append(name)
            out.append(dict(f, card_id=card.id, model=card.model, kind=card.kind, suggested=name,
                            how="the instrument collection's card"))
            log.info("  %s: the instrument collection has a card for it, %s", f["address"], card.id)
        if out:
            self.runtime.refresh_cards([f["card_id"] for f in out])
        return out

    def _write_cards(self, unknown: List[Dict[str, Any]], matched: List[Dict[str, Any]]) -> Any:
        """For instruments that answered but match no card: the instrument collection's card, once it is up to date;
        else find the manual (the collection's link first) and write a draft card (autocard.py)."""
        written = self._from_collection(unknown, matched)
        unknown = [f for f in unknown if f["address"] not in {w["address"] for w in written}]
        if not unknown or self.make_card is None or self.settings.get("auto_cards") == "off":
            return written, []
        from .autocard import maker_model
        from .discovery import Found
        problems = self._card_problems
        taken = [f.get("suggested") for f in matched + written if f.get("suggested")]
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

    def _theirs_meanwhile(self, started: Any) -> bool:
        """Whether a person set up their own lab while this set-up scanned and wrote cards (Save and connect, a lab
        config of their own): then the set-up's draft never takes its place."""
        rt = self.runtime
        return rt.lab is not started or rt.lab_info.get("kind") == "file" or self.lab_file.exists()

    def _connect(self, matched: List[Dict[str, Any]], started: Any = None) -> bool:
        """Write the observed lab and connect to it. False (and a status saying why) if that is not possible now.

        The lab config is written only as a new file, never over one a person saved meanwhile (their ±0.5 V limit once
        became the maker's ±210 V, and the next start opened Kelvin's draft), and the lab is switched only while it is
        still the one the set-up started from (`started`). The lab opens before it connects, then connects instrument
        by instrument (connect_pending), as at every other start: one that can't connect (a COM port another program
        holds) is set aside with why, and the rest are used. All or nothing once left the whole found lab out."""
        from .homelab import build_lab, lab_registry
        rt = self.runtime
        started = rt.lab if started is None else started
        registry = lab_registry(self.home, rt.collection)
        text = draft_found(matched, registry)
        names = ", ".join("%s (%s)" % (f.get("suggested"), f.get("model")) for f in matched)
        self._set("connecting", message="Found %s; connecting" % names, found=len(matched))
        kept = ("Found %s; you saved your own lab config meanwhile, so Kelvin left it as it is (Set up's Scan finds "
                "these again)" % names)
        if self._theirs_meanwhile(started) or not _write_new(self.lab_file, text):
            log.info("Instrument setup: %s", kept)
            self._set("done", found=len(matched), message=kept)
            return False
        proposed = self._keep_proposed(text, registry)
        changed = getattr(rt, "config_changed", None)   # kept in the lab's config history (configversions.py)
        if changed is not None:
            changed("Kelvin (setup)", "set up the instruments it found: %s" % names, "setup")
        if not self.can_switch():   # Kelvin is answering: the switch waits for the answer to end (switch_lab idle)
            self._set("connecting", message="Found %s; Kelvin switches to them when it is next idle" % names,
                      found=len(matched))
        try:   # opened before it connects: nothing reaches an instrument here, so nothing here can fail on one
            lab = build_lab(self.lab_file, None, 0, registry, getattr(self.settings, "switched_off", None), later=True)
        except Exception as e:   # the config itself (a card that can't be read): no lab to switch to
            self._set("done", found=len(matched), message="Found %s but could not open them as a lab: %s"
                      % (names, profiles._first_line(e)))
            return False
        info = {"kind": "file", "path": str(self.lab_file), "error": None, "has_file": True, "auto": True}
        if rt.lab is not started or rt.lab_info.get("kind") == "file":   # a person's own lab came in meanwhile
            lab.close()
            self._set("done", found=len(matched), message=kept)
            return False
        try:   # never under an answer in progress: it once ran a whole turn on the lab it left meanwhile
            rt.switch_lab(lab, None, info, "Kelvin auto-setup", idle=self.can_switch)
        except RuntimeError as e:
            lab.close()
            self._set("done", found=len(matched), message="Found %s; %s" % (names, e))
            return False
        # switch_lab connected them, one by one, setting aside with why one that couldn't be (or didn't open in time)
        away = {n: why for n, why in rt.lab.unavailable.items() if n in rt.lab.config.instruments}
        self._missing = dict(getattr(self, "_missing", {}), **away)
        connected = sorted(n for n, i in rt.lab.instruments.items() if i.enabled)
        self._set("connected", message="Connected %s%s" % (", ".join(connected) or "nothing", (
            "; not connected: " + "; ".join("%s (%s)" % (n, profiles._first_line(why)) for n, why in sorted(away.items()))
            ) if away else ""), connected=connected, not_connected=sorted(away), found=len(matched))
        missing = sorted(away)
        rt.notifier("setup", "Kelvin set up %d instrument%s" % (len(matched), "" if len(matched) == 1 else "s"),
                    names + ". Readings and settings work now; Kelvin asks you before each output change until you "
                    "review their limits in Lab setup.%s%s" % (
                        " Not connected: %s (the Lab panel says why; connect it again from there once that is fixed)."
                        % ", ".join(missing) if missing else "",
                        " Your Special Measure rack's limits for %s are there for you to accept (Limits… on %s row)."
                        % (", ".join(proposed), "its" if len(proposed) == 1 else "each") if proposed else ""))
        return True

    def _keep_proposed(self, text: str, registry: Any) -> List[str]:
        """What the lab's Special Measure rack proposes for each instrument of the first lab config it has
        (specialmeasure.on_card: its channels' limits and ramp rates, the compliance its setup sets), kept in the
        instrument's folder for the person: its review shows it, and only a person sets limits. The names kept for."""
        import copy
        from . import specialmeasure
        from .yamlio import load_yaml
        with self._scripts_lock:
            rack = dict(self.racks)
        if not rack:
            return []
        try:
            entries = (load_yaml(text) or {}).get("instruments") or {}
        except Exception:   # Kelvin wrote it, so it reads; never a reason to stop the setup
            entries = {}
        kept: List[str] = []
        for name, e in entries.items():
            hit = rack.get(specialmeasure.rack_key(str(e.get("address") or ""))) if isinstance(e, dict) else None
            if hit is None or str(e.get("card")) not in registry:
                continue
            limits, rates = specialmeasure.on_card(copy.deepcopy(hit[0]), registry.get(str(e["card"])))
            if not limits and not rates:
                continue
            try:
                specialmeasure.keep_proposed(self.home, str(name), str(e["address"]), str(e["card"]), limits, rates,
                                             hit[1])
                kept.append(str(name))
            except (KeyError, OSError) as err:
                log.info("  could not keep the rack's limits for %s: %s", name, profiles._first_line(err))
        return kept
