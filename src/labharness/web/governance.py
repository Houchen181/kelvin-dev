"""The portal's side of the lab's governance: the config history (an audit view with diffs and rollback), the lab's
roles on top of the owner's PIN, and where the lab owner hears of safety changes.

People only, on the Kelvin computer: no agent tool reaches these, and no phone (remote.py lists none of them). The
rules are accounts.LabRoles' and configversions.py's; this file only serves them."""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..accounts import ROLES as LAB_ROLES
from .. import carddraft
from ..configversions import LAB, REVIEWS, Unreadable, widens


def routes(runtime: Any, settings: Any, accounts: Any, needs_owner: Callable[..., Optional[JSONResponse]],
           lab_post: Callable[..., Any], person: Callable[[Request], str],
           configure_notifications: Callable[[], None], busy_problem: Callable[[], Optional[str]]) -> List[Route]:
    """needs_owner(body, what): the PIN check (and, with roles, a trainee's refusal); lab_post(body, by, why,
    rollback): saving and connecting a lab config as Lab setup's Save does; configure_notifications: after the
    owner's notices changed."""

    def error(status: int, message: str, **more: Any) -> JSONResponse:
        return JSONResponse(dict(more, error=message), status_code=status)

    def entry_view(e: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in e.items() if k != "sha"}
        if e.get("type") == "version":
            out["gone"] = not e.get("sha")
        return out

    async def history(request: Request) -> Response:
        """GET ?subject=&safety=1&who=&limit=: the lab's config history, newest first: each version of the lab
        config and of the lab's cards, and each safety change that is no file (auto mode on, Hands on the sample
        unlocked, the roles), with who, when, why and what changed. subject: lab.yaml, cards, events or a card's
        file (cards/<name>.yaml)."""
        if runtime.versions is None:
            return error(404, "This Kelvin keeps no lab config history (the Kelvin app does).")
        await asyncio.to_thread(runtime.config_noticed)   # an edit made by hand shows as a change of its own first
        q = request.query_params
        try:
            limit = max(1, min(int(q.get("limit") or 200), 1000))
        except ValueError:
            limit = 200
        entries = runtime.versions.history(str(q.get("subject") or ""), q.get("safety") in ("1", "true", "yes"),
                                           str(q.get("who") or ""), limit)
        everyone = runtime.versions.entries()
        people = sorted({str(e.get("account") or e.get("by") or "") for e in everyone} - {""}, key=str.lower)
        files = sorted({str(e.get("subject")) for e in everyone if e.get("type") == "version"})
        return JSONResponse({"entries": [entry_view(e) for e in entries], "people": people, "files": files,
                             "folder": str(runtime.versions.folder), "role": runtime.role()})

    async def version(request: Request) -> Response:
        """GET ?against=m: one version: its changes in words, the diff against the version of the same file before
        it (or against m), and its text."""
        if runtime.versions is None:
            return error(404, "This Kelvin keeps no lab config history.")
        try:
            n = int(request.path_params["n"])
            against = request.query_params.get("against")
            out = runtime.versions.diff(n, int(against) if against else None)
            out["entry"] = entry_view(runtime.versions.get(n))
        except (KeyError, ValueError) as e:
            return error(404, e.args[0] if e.args else str(e))
        return JSONResponse(out)

    async def rollback(request: Request) -> Response:
        """POST {n, why, pin}: put version n of its file back. A change like any other: the lab config is saved and
        connected as Lab setup's Save does (the owner's PIN, when set); a card, or Kelvin's cards' reviews, needs
        the PIN when going back widens Kelvin's reach (a wider limit, a card marked reviewed). With roles, never a
        trainee's. Kept as a new version that says which one it put back."""
        if runtime.versions is None:
            return error(404, "This Kelvin keeps no lab config history.")
        body = await request.json()
        try:
            n = int(body.get("n"))
            subject, text = runtime.versions.restore(n)
        except (KeyError, TypeError, ValueError) as e:
            return error(404, e.args[0] if e.args and isinstance(e.args[0], str) else "no such version")
        by = person(request)
        why = "rolled back to #%d%s" % (n, ": " + " ".join(str(body.get("why")).split()) if body.get("why") else "")
        if subject == LAB:
            if text is None:
                return error(400, "In #%d there was no lab config. To leave your instruments, choose the virtual lab "
                                  "in Lab setup." % n)
            try:
                if text == runtime.versions.now(LAB):
                    return error(409, "The lab config is #%d's already." % n)
            except Unreadable:
                pass   # Save and connect writes it whole
            return await lab_post({"yaml": text, "pin": body.get("pin")}, by, why, n)
        refused = needs_owner(body, "Rolling the lab's cards back", owner=False)   # a trainee: never
        if refused:
            return refused
        try:
            now = runtime.versions.now(subject)
        except Unreadable as e:
            return error(409, "Kelvin can't read %s now (%s); try again in a moment." % (subject, e))
        if now == text:
            return error(409, "%s is #%d's already." % (subject, n))
        if widens(subject, now, text):   # going back widens Kelvin's reach: the owner's, as the change itself was
            refused = needs_owner(body, "Rolling the lab's cards back")
            if refused:
                return refused
        problem = busy_problem()
        if problem:
            return error(409, problem)
        path = runtime.versions.path_of(subject)
        ids: List[str] = []
        if subject != REVIEWS:
            for t in (now, text):
                card = carddraft.check(t)[0] if t is not None else None
                if card is not None:
                    ids.append(card.id)
            if text is not None and carddraft.check(text)[0] is None:
                return error(400, "#%d of %s isn't a card Kelvin can use now: %s" % (
                    n, subject, "; ".join(carddraft.check(text)[1][:3])))
        else:
            import json
            for t in (now, text):
                try:
                    ids += list(json.loads(t or "{}"))
                except ValueError:
                    pass
        try:
            if text is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, path)
        except OSError as e:
            return error(500, "Could not put #%d back: %s" % (n, e))
        runtime.refresh_cards(sorted(set(ids)))
        for inst in runtime.lab.instruments.values():   # a review going back holds at once, as Mark reviewed does
            if inst.card.id in ids and inst.card.id in runtime.lab.registry:
                inst.card.status = runtime.lab.registry.get(inst.card.id).status
        made = await asyncio.to_thread(runtime.config_changed, by, why, "rollback", n)
        return JSONResponse({"ok": True, "versions": [v["n"] for v in made], "reconnect": sorted(
            name for name, inst in runtime.lab.instruments.items() if inst.card.id in ids and subject != REVIEWS)})

    def roles_data() -> Dict[str, Any]:
        roles = runtime.roles.all() if runtime.roles is not None else {}
        people = [a["name"] for a in accounts.list()] if accounts is not None else []
        target = runtime.roles.notify_target() if runtime.roles is not None else {}
        email = target.get("email") if isinstance(target.get("email"), dict) else {}
        hook = str(target.get("webhook") or "")
        where = [w for w in (hook.split("/")[2] if "://" in hook else "", str(email.get("to") or "")) if w]
        return {"on": bool(runtime.roles is not None and runtime.roles.on), "roles": roles,
                "people": [{"name": p, "role": runtime.roles.role(p) if runtime.roles is not None and
                            runtime.roles.on else None, "given": p in roles} for p in people],
                "accounts": accounts is not None, "pin": bool(runtime.owner is not None and runtime.owner.is_set),
                "me": runtime.user, "my_role": runtime.role(), "owner_notify": where,
                "roles_list": list(LAB_ROLES)}

    async def roles_view(request: Request) -> Response:
        """GET: the lab's roles: each account on this computer with its role (an account the owner gave none is a
        trainee while there are roles), whether the PIN is set, where the owner hears of safety changes. POST
        {roles: {name: owner | operator | trainee | ""}, pin}: give the roles ({} or all empty: no roles, everyone
        equal again). Needs the owner's PIN, which must be set; once there are roles, an owner signed in."""
        if request.method == "GET":
            return JSONResponse(roles_data())
        if runtime.roles is None or accounts is None:
            return error(404, "Roles need accounts: each person signs in to Kelvin on this computer.")
        body = await request.json()
        roles = body.get("roles") if isinstance(body.get("roles"), dict) else {}
        try:
            given = runtime.roles.set(roles, runtime.owner, body.get("pin"), runtime.user, accounts)
        except PermissionError as e:
            pin = "PIN" in str(e)
            return error(403, str(e), **({"pin": True} if pin else {}))
        except ValueError as e:
            return error(400, str(e))
        by = person(request)
        runtime.gate.log.write("lab_roles", by=by, account=runtime.user, roles=given)
        what = "lab roles: %s" % (", ".join("%s %s" % kv for kv in sorted(given.items())) if given
                                  else "none, everyone equal again")
        await asyncio.to_thread(runtime.audit_event, what, by, "", not given, roles=given)
        if runtime.role() == "trainee" and runtime.gate.auto:   # auto mode is not a trainee's
            await asyncio.to_thread(runtime.set_approval_mode, "ask", "lab roles")
        return JSONResponse(roles_data())

    async def owner_notify(request: Request) -> Response:
        """POST {use: "mine" | "off", pin}: the owner's safety notices go where the signed-in person's own
        notifications go (a copy of their webhook and email settings, kept with the roles, readable on this
        computer), or only to the in-app list. The owner's PIN; with roles, an owner signed in."""
        if runtime.roles is None:
            return error(404, "This Kelvin keeps no lab roles.")
        body = await request.json()
        target: Optional[Dict[str, Any]] = None
        if body.get("use") == "mine":
            mail = settings.email()
            target = {"webhook": settings.get("webhook") or None,   # as notify.email_from takes it
                      "email": {"host": mail.host, "to": ", ".join(mail.to), "port": mail.port,
                                "security": mail.security, "user": mail.user, "password": mail.password,
                                "sender": mail.sender} if mail else None}
            if not target["webhook"] and not target["email"]:
                return error(400, "You have no webhook or email for notifications yet: set one up in Settings, "
                                  "Notifications, then come back.")
        try:
            runtime.roles.set_notify(target, runtime.owner, body.get("pin"), runtime.user)
        except PermissionError as e:
            return error(403, str(e), **({"pin": True} if "PIN" in str(e) else {}))
        except ValueError as e:
            return error(400, str(e))
        configure_notifications()
        by = person(request)
        await asyncio.to_thread(runtime.audit_event, "the owner's safety notices %s" % (
            "go to %s's notification settings" % (runtime.user or "the person signed in") if target else
            "go only to the in-app list"), by, "", target is None)
        return JSONResponse(roles_data())

    return [
        Route("/api/config/history", history),
        Route("/api/config/history/{n:int}", version),
        Route("/api/config/rollback", rollback, methods=["POST"]),
        Route("/api/roles", roles_view, methods=["GET", "POST"]),
        Route("/api/roles/notify", owner_notify, methods=["POST"]),
    ]
