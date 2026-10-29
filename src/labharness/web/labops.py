"""The portal's lab-ops basics (F3 #20, #25): Help and the first-run checklist, a run's or a day's record for an ELN,
each instrument's calibration, and approval cards in the reader's language. app.py mounts these routes; they are for
the Kelvin computer (phones get a card's translation with the lab's state)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, List
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .. import cardlang, eln, guide, profiles

MAX_SHOWN = 6   # drives listed by name in the checklist, the rest counted


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def checklist(runtime: Any, settings: Any, home: Any) -> List[Dict[str, Any]]:
    """What a lab needs before Kelvin is trusted with it, each with done or not, why, and where to do it (action:
    lab, cards, pin, phone, data). Read from what Kelvin keeps; nothing is asked of an instrument."""
    lab = runtime.lab
    real = {n: i for n, i in lab.instruments.items() if not i.simulated}
    out: List[Dict[str, Any]] = []

    def item(key: str, title: str, done: bool, detail: str, action: str) -> None:
        out.append({"key": key, "title": title, "done": bool(done), "detail": detail, "action": action})

    mine = (runtime.lab_info or {}).get("kind") == "file" and bool(real)
    item("lab", "Your lab is set up", mine,
         "Kelvin controls %s: %d instrument%s of your own." % (lab.config.name, len(real), "" if len(real) == 1
                                                                 else "s") if mine else
         "Kelvin is on the virtual lab, whose instruments are simulated. Lab → Set up… finds your own instruments "
         "and writes the lab config.", "lab")

    missing = sorted(n for n in lab.unavailable if n in real)
    drafts = sorted(n for n, i in real.items() if i.enabled and i.card.status == "draft")
    why = ["%s isn't connected" % n for n in missing] + ["%s's card is a draft nobody has reviewed (Lab → Cards…)" % n
                                                          for n in drafts]
    item("instruments", "Instruments checked", bool(real) and not why,
         "; ".join(why) + "." if why else ("Every instrument answers, on a card a person reviewed." if real else
                                           "No instruments of your own yet."), "cards" if drafts else "lab")

    unreviewed = list(lab.config.unreviewed()) if real else []
    card_only = ["%s.%s" % (n, s.name) for n, i in real.items() for s in i.card.drives
                 if s.numeric and i.enabled and not i.config.limits.get(s.name)]
    why = []
    if unreviewed:
        why.append("Nobody has reviewed the limits of %s, which Kelvin added itself." % ", ".join(unreviewed))
    if card_only:
        named = ", ".join(card_only[:MAX_SHOWN]) + (" and %d more" % (len(card_only) - MAX_SHOWN)
                                                    if len(card_only) > MAX_SHOWN else "")
        why.append("On the card's limits, the maker's full range: %s. Set the lab's own with Limits… on each row "
                   "(Lab → Set up…)." % named)
    item("limits", "Limits set for your setup", bool(real) and not why, " ".join(why) if why else
         ("Every output has the lab's own limits." if real else "No instruments of your own yet."), "lab")

    owner = getattr(runtime, "owner", None)
    pin = bool(owner is not None and owner.is_set)
    item("pin", "Lab owner's PIN", pin,
         "Set: limits, auto mode and unlocking Hands on the sample need it." if pin else
         "Not set: anyone at this computer can widen limits or switch on auto mode. Settings → Instruments → Lab "
         "owner's PIN.", "pin")

    phones = settings.devices
    item("phone", "A phone paired", bool(phones),
         "%d paired: approvals and alarms reach you away from the computer." % len(phones) if phones else
         "None yet: pair one in Settings → Phone, so a request or an alarm in the night reaches you.", "phone")

    data = Path(runtime.data_dir)
    default = Path(home) / "virtual-lab"
    chosen = data.resolve() != default.resolve()
    item("data", "Data folder", chosen,
         "Runs, the lab record and reports are saved in %s." % data if chosen else
         "Runs are saved in %s, the virtual lab's folder. Choose your own (a lab server or a synced drive works) in "
         "Settings → Data & reports." % data, "data")
    return out


def routes(runtime: Any, settings: Any, home: Any, who: Callable[[Request], str]) -> List[Route]:
    """The routes, for app.py's list. who(request): the person asking, as the lab's record names them."""
    home = Path(home)

    async def help_view(request: Request) -> Response:
        """GET ?q=words: the passages of Kelvin's documentation that answer them; ?section=doc.md › Heading: that
        section whole; neither: the sections to browse, with the first-run checklist."""
        q, section = request.query_params.get("q", ""), request.query_params.get("section", "")
        if section:
            text = await asyncio.to_thread(guide.section, section)
            return JSONResponse({"section": section, "text": text}) if text else _error(404, "no such section")
        if q.strip():
            return JSONResponse({"q": q, "passages": await asyncio.to_thread(guide.lookup, q)})
        return JSONResponse({"topics": await asyncio.to_thread(guide.topics)})

    async def checklist_view(request: Request) -> Response:
        """GET: the first-run checklist. POST {hidden}: a person hides it from the start screen (Help keeps it)."""
        if request.method == "POST":
            body = await request.json()
            settings.update({"checklist": "hidden" if body.get("hidden") else ""})
        items = await asyncio.to_thread(checklist, runtime, settings, home)
        return JSONResponse({"items": items, "done": sum(1 for i in items if i["done"]), "total": len(items),
                             "hidden": settings.public().get("checklist") is False})

    async def cardtext(request: Request) -> Response:
        """POST {title, items, where, language}: a card as this person reads it (cardlang.py), shown as it comes
        in; the card itself, its answer and the record stay as Kelvin wrote them."""
        body = await request.json()
        items = body.get("items") if isinstance(body.get("items"), list) else []
        language = str(body.get("language") or settings.card_language)
        return JSONResponse(cardlang.card(str(body.get("title") or "")[:2000], [str(i)[:20000] for i in items[:60]],
                                          body.get("where"), language))

    def real(name: str) -> Any:
        inst = runtime.lab.instruments.get(name)
        if inst is None or inst.simulated:
            raise KeyError("no instrument %r on this computer" % name)
        return inst

    async def calibration_view(request: Request) -> Response:
        """GET: an instrument's calibration on record and where it stands. POST {due, calibrated, certificate, file,
        provider, serial, note}: a person records a calibration (kept with the earlier ones)."""
        name = request.path_params["name"]
        try:
            real(name)
            if request.method == "POST":
                body = await request.json()
                record = profiles.record_calibration(
                    home, name, body.get("due"), body.get("calibrated") or "", str(body.get("certificate") or ""),
                    who(request), str(body.get("serial") or ""), str(body.get("provider") or ""),
                    str(body.get("note") or ""), str(body.get("file") or ""))
                runtime.gate.log.write("calibration", by=who(request), instrument=name, due=record["due"],
                                       calibrated=record.get("calibrated"), certificate=record.get("certificate"))
            return JSONResponse(dict(profiles.calibration_view(home, name), name=name))
        except KeyError as e:
            return _error(404, e.args[0])
        except ValueError as e:
            return _error(400, str(e))

    async def certificate_view(request: Request) -> Response:
        """PUT (the file as the body, its name in X-File-Name): keep a calibration certificate in the instrument's
        folder, for the record that names it. GET ?file=: open one."""
        name = request.path_params["name"]
        try:
            real(name)
            if request.method == "PUT":
                data = await request.body()
                kept = await asyncio.to_thread(profiles.save_certificate, home, name,
                                               unquote(request.headers.get("x-file-name", "")), data)
                return JSONResponse({"file": kept})
            path = profiles.certificate_path(home, name, request.query_params.get("file", ""))
        except KeyError as e:
            return _error(404, e.args[0])
        except ValueError as e:
            return _error(400, str(e))
        return FileResponse(path, media_type=profiles.CERT_KINDS.get(path.suffix.lower(), "application/octet-stream"),
                            filename=path.name)

    async def eln_view(request: Request) -> Response:
        """A run's (/api/runs/{id}/eln) or a day's (/api/eln?day=YYYY-MM-DD) record as Markdown with its figures,
        as a .zip to put in an ELN. It is kept in the data folder's eln/ too."""
        run_id = request.path_params.get("run_id")
        day = "" if run_id else request.query_params.get("day", "today")
        try:
            result = await asyncio.to_thread(eln.export, runtime, [run_id] if run_id else None, day, 0, who(request))
            data = await asyncio.to_thread(eln.zipped, result["folder"])
        except KeyError as e:
            return _error(404, e.args[0] if e.args else "no such run")
        except ValueError as e:
            return _error(400, str(e))
        except OSError as e:
            return _error(500, "Could not write the record: %s" % e)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="%s.zip"' % result["name"],
                                 "X-Kelvin-Saved": json.dumps(result["path"])})

    return [
        Route("/api/help", help_view),
        Route("/api/checklist", checklist_view, methods=["GET", "POST"]),
        Route("/api/cardtext", cardtext, methods=["POST"]),
        Route("/api/instruments/{name}/calibration", calibration_view, methods=["GET", "POST"]),
        Route("/api/instruments/{name}/calibration/certificate", certificate_view, methods=["GET", "PUT"]),
        Route("/api/runs/{run_id}/eln", eln_view),
        Route("/api/eln", eln_view),
    ]
