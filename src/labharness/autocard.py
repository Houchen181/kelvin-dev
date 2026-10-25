"""Kelvin writes the card for an instrument it does not know: it finds the maker's manual, drafts the card from the
manual's command reference with your model, and makes it safe to try whatever the model wrote.

    1. The manual. Addresses your model suggests on the maker's own site, and known patterns for some makers. A web
       page is followed one step to the PDFs it links. A document counts only once Kelvin has downloaded it and its
       text names the model; of those, the one with the most command syntax wins.
    2. The card. The model writes it from the command reference (the pages densest in commands), the card checker
       reviews it, and the model fixes what the checker found, once.
    3. Safe to try. Status draft: on real hardware every setting and output change asks a person, saying the card
       is a draft (gate.py). No commands are sent on connect. Reads that start something (MEASure?, READ?, INITiate,
       *TST?, *CAL?: on some instruments they switch an output on or run a self-test) are left out. The identity
       pattern is checked against the instrument's own reply; for an instrument that gave none (the person named
       it), the manual's identity check stays as written, for try_card to test.

Only the maker and the model name leave the computer (to your model and the maker's site), never the serial number.
A person reviews the card against the manual and marks it reviewed; until then nothing on it changes without them."""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from . import carddraft, profiles
from .scpi import is_query

Ask = Callable[[str, str], Tuple[str, str]]       # (system, prompt) -> (answer, model key)
Fetch = Callable[[str], bytes]

MAX_FETCHES = 10
GOOD = 40          # command-syntax hits that make a document a command reference
# reads that do something: measure (and on some source-meters switch the output on), trigger, self-test, calibrate
ACTION_READ = re.compile(r"^[\s:]*(MEAS(?:ure)?|READ|INIT(?:iate)?|\*CAL|\*TST|\*TRG|TRIG(?:ger)?)\b", re.I)
_CMD = re.compile(r"(?<![A-Za-z0-9])(\*[A-Z]{3}\??|:?[A-Z]{3,}[a-z]*(?::[A-Z]{2,}[a-z]*)+\??|[A-Z]{3,5}\?)")
KNOWN = [   # makers whose manuals sit at a predictable address
    (re.compile(r"stanford|\bsrs\b", re.I), "https://www.thinksrs.com/downloads/pdfs/manuals/{MODEL}m.pdf"),
]
SUGGEST_SYSTEM = ("You help a lab assistant find instrument manuals. Answer with web addresses only, one per line, "
                  "no other text.")


def maker_model(idn: str) -> Tuple[str, str]:
    p = profiles.parse_idn(idn)
    return p.get("maker", "").strip(), p.get("model", "").strip()


def suggest_prompt(maker: str, model: str) -> str:
    return ("Where can the official manual of the %s %s be downloaded? I need its programming (remote control, "
            "SCPI or command reference) manual, ideally a PDF. Give up to 6 addresses on %s's own website, the most "
            "likely first; a product or downloads page that links the manual is fine too. Only addresses you are "
            "confident exist." % (maker, model, maker))


def urls_in(text: str) -> List[str]:
    out = []
    for u in re.findall(r"https?://[^\s<>\"'`]+", text or ""):
        u = u.rstrip(".,;)]}>")
        if u not in out:
            out.append(u)
    return out


def command_score(text: str) -> int:
    return len(_CMD.findall(text or ""))


def names_model(text: str, model: str) -> bool:
    """Does the document name this model? Every word of it that has a digit must appear (else its longest word)."""
    low = (text or "").lower()
    words = [w for w in re.findall(r"[a-z0-9]+", model.lower()) if len(w) >= 3]
    if not words:
        return False
    needed = [w for w in words if any(c.isdigit() for c in w)] or [max(words, key=len)]
    return all(w in low for w in needed)


def _site(url: str) -> str:
    host = urlparse(url).hostname or ""
    return ".".join(host.split(".")[-2:])


def pdf_links(html: str, base: str) -> List[str]:
    """Links on a maker's page that lead to manuals: PDFs, or links that say manual, on the same site."""
    out = []
    for href, label in re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, re.I | re.S):
        url = urljoin(base, href.strip())
        text = re.sub(r"<[^>]+>", " ", label).lower()
        if not url.startswith(("https://", "http://")) or _site(url) != _site(base):
            continue
        if url.lower().split("?")[0].endswith(".pdf") or re.search(r"manual|programm|reference|command", text + url.lower()):
            if url not in out:
                out.append(url)
    return out[:8]


def find_manual(maker: str, model: str, ask: Optional[Ask], fetch: Fetch,
                log: Callable[[str], None] = lambda m: None,
                problems: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """The best manual Kelvin could download and check for this model, or None (and why, in `problems`)."""
    queue: List[Tuple[str, int]] = []
    compact = re.sub(r"[^A-Za-z0-9]", "", model).upper()
    for rx, pattern in KNOWN:
        if rx.search(maker):
            queue.append((pattern.format(MODEL=compact), 0))
    if ask is not None:
        try:
            answer, _ = ask(SUGGEST_SYSTEM, suggest_prompt(maker, model))
            queue += [(u, 0) for u in urls_in(answer)[:6]]
        except Exception as e:
            log("could not ask the model for the manual's address: %s" % profiles._first_line(e))
            if problems is not None:
                problems.append(profiles._first_line(e))
    best: Optional[Dict[str, Any]] = None
    seen, fetches = set(), 0
    while queue and fetches < MAX_FETCHES:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        fetches += 1
        try:
            data = fetch(url)
        except Exception as e:
            log("%s: %s" % (url, profiles._first_line(e)))
            continue
        if data[:5] == b"%PDF-":
            try:
                pages = profiles.pdf_pages(data)
            except Exception as e:
                log("%s: not readable (%s)" % (url, profiles._first_line(e)))
                continue
            kind = "pdf"
        elif b"<html" in data[:4096].lower():
            html = data.decode("utf-8", errors="replace")
            if depth == 0:
                queue = [(u, 1) for u in pdf_links(html, url)] + queue   # a product page: look at what it links
            from .literature import html_to_text
            pages = [html_to_text(html)]
            kind = "html"
        else:
            continue
        head = "\n".join(pages[:20])
        if not names_model(head, model):
            log("%s: does not name the %s" % (url, model))
            continue
        score = sum(command_score(p) for p in pages)
        if kind == "html" and sum(len(p) for p in pages) < 5000:
            continue
        found = {"url": url, "pages": pages, "score": score, "kind": kind, "data": data}
        if best is None or score > best["score"]:
            best = found
        if score >= GOOD and kind == "pdf":
            break
    return best


def command_excerpt(pages: List[str], limit: int = carddraft.MAX_MANUAL) -> str:
    """The pages densest in commands, in their own order, up to `limit` characters, each marked with its page."""
    ranked = sorted(range(len(pages)), key=lambda i: -command_score(pages[i]))
    chosen, size = [], 0
    for i in ranked:
        if command_score(pages[i]) == 0:
            break
        text = pages[i].strip()
        if size + len(text) > limit:
            continue
        chosen.append(i)
        size += len(text) + 12
    return "\n\n".join("[page %d]\n%s" % (i + 1, pages[i].strip()) for i in sorted(chosen))


def make_safe(text: str, maker: str, model: str, idn: str, url: str, model_key: str) -> Tuple[str, List[str]]:
    """The card as Kelvin keeps it, whatever the model wrote (see the module's notes), and what was changed."""
    import yaml
    data = yaml.safe_load(carddraft.extract_yaml(text))
    if not isinstance(data, dict):
        raise ValueError("the model did not answer with a card")
    changed: List[str] = []
    data["status"] = "draft"
    if data.pop("on_connect", None):
        changed.append("left out the commands on connect")
    ident = data.get("identify") if isinstance(data.get("identify"), dict) else {}
    pattern = str(ident.get("idn") or "")
    if idn:
        try:
            fits = bool(pattern) and re.search(pattern, idn, re.I) is not None
        except re.error:
            fits = False
        if not fits:
            ident["idn"] = "^%s\\s*,\\s*%s\\b" % (re.escape(maker), re.escape(model))
            changed.append("set the identity pattern from the instrument's own reply")
        ident["example"] = idn
    elif pattern:   # no reply to check it against: the manual's says, try_card tests it
        changed.append("kept the manual's identity check (%s %r), untested: try_card tests it"
                       % (ident.get("query") or "*IDN?", pattern))
    if ident:
        data["identify"] = ident
    else:
        data.pop("identify", None)
    params = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
    for name in list(params):
        p = params[name] if isinstance(params[name], dict) else {}
        get = str(p.get("get") or "")
        if get and (not is_query(get) or ACTION_READ.match(get)):
            p.pop("get", None)
            changed.append("left out the read %r of %s (it may start something)" % (get, name))
            if p.get("read_when"):
                p.pop("read_when")
        if not p.get("get") and not p.get("set"):
            params.pop(name)
    data["parameters"] = params
    display = data.get("display") if isinstance(data.get("display"), dict) else {}
    if display.get("headline") and (display["headline"] not in params or not params[display["headline"]].get("get")):
        data.pop("display", None)
    data["manual_url"] = url if url.startswith("https://") else ""
    data.setdefault("manual", "%s %s manual" % (maker, model))
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    blocked = list(raw.get("blocked") or [])
    for b in ("CALibration", "SYSTem:FIRMware"):
        if b not in blocked:
            blocked.append(b)
    raw["blocked"] = blocked
    data["raw"] = raw
    head = ("# Drafted by Kelvin on %s from %s with %s.\n# A draft: check every command, limit and safe value "
            "against the manual, then mark it reviewed in Lab setup.\n" % (time.strftime("%Y-%m-%d"), url, model_key))
    return head + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110), changed


def draft(manual_pages: List[str], maker: str, model: str, idn: str, url: str, ask: Ask) -> Tuple[str, List[str], str]:
    """Write the card with the model, check it, and have the model repair it once. Returns (card text, changes, model)."""
    name = ("%s %s (it answers *IDN? with %r)" % (maker, model, ",".join(idn.split(",")[:2])) if idn else
            "%s %s (as the person named it; Kelvin has no identity reply from it)" % (maker, model))
    answer, key = ask(carddraft.DRAFT_SYSTEM, carddraft.draft_prompt(command_excerpt(manual_pages), name))
    text, changed = make_safe(answer, maker, model, idn, url, key)
    card, problems, _ = carddraft.check(text)
    if problems:
        answer, key = ask(carddraft.DRAFT_SYSTEM, carddraft.repair_prompt(text, problems))
        text, changed = make_safe(answer, maker, model, idn, url, key)
        card, problems, _ = carddraft.check(text)
    if problems:
        raise ValueError("the card the model wrote still has problems: " + "; ".join(problems[:4]))
    return text, changed, key


def make_card(found: Dict[str, Any], cards_dir: Any, ask: Optional[Ask], fetch: Optional[Fetch] = None,
              builtin_ids: Any = (), log: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    """For an instrument that matches no card: find its manual, write its card, keep it as a draft. Maker and model
    come from its identity reply, else from found's maker and model (what the person said it is).
    Returns {card_id, model, kind, manual: {url, data, kind}, changed, parameters} or {error}."""
    fetch = fetch or profiles.http_get
    idn = str(found.get("idn") or "")
    maker, model = maker_model(idn) if idn else ("", "")
    if not maker or not model:
        maker = maker or str(found.get("maker") or "").strip()
        model = model or str(found.get("model") or "").strip()
    if not maker or not model:
        return {"error": "its identity reply %r does not say maker and model" % idn[:60] if idn else
                "Kelvin needs the maker and the model to find the manual"}
    if ask is None:
        return {"error": "Kelvin needs a model to write the %s %s card: add an API key in Settings" % (maker, model)}
    asked: List[str] = []
    manual = find_manual(maker, model, ask, fetch, log, asked)
    if manual is None and asked:
        return {"error": "Kelvin could not ask a model about the %s %s (%s)" % (maker, model, asked[0])}
    if manual is None:
        return {"error": "Kelvin could not find the %s %s manual online: choose its PDF under Make a card for "
                         "it from its manual" % (maker, model)}
    if manual["score"] < 15:
        return {"error": "the %s %s manual Kelvin found (%s) has no command reference: choose the programming "
                         "manual's PDF in Lab setup" % (maker, model, manual["url"])}
    try:
        text, changed, key = draft(manual["pages"], maker, model, idn, manual["url"], ask)
        card, path, _ = carddraft.save(cards_dir, text, builtin_ids, by="kelvin")
    except Exception as e:
        return {"error": "Kelvin could not write the %s %s card: %s" % (maker, model, profiles._first_line(e))}
    log("wrote %s from %s with %s (%s)" % (card.id, manual["url"], key, "; ".join(changed) or "as drafted"))
    params = ", ".join("%s (%s%s)" % (p.name, "output" if p.is_drive else "setting" if p.set_cmd else "reading",
                                      ", " + p.unit if p.unit else "") for p in card.params.values())
    return {"card_id": card.id, "model": card.model, "kind": card.kind, "changed": changed, "by": key,
            "manual": {"url": manual["url"], "data": manual["data"], "kind": manual["kind"]}, "path": str(path),
            "parameters": params}
