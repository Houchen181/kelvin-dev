"""Each instrument's working folder: who it is, how Kelvin talks to it, its settings, and its manual.

    ~/Kelvin/instruments/<name>/
        instrument.json   identity (the *IDN? reply split into maker, model, serial number, firmware), card,
                          address, the connection settings that answered, when it was found, where the manual is from
        settings.json     every setting and reading at setup (ranges, time constants, sensitivities...), refreshed
                          each time Kelvin connects
        manual.pdf        the maker's manual, fetched once from the address on the card (or put there by hand)
        manual.txt        its text, page by page, which search_manual looks through

Kelvin reads these when it plans an involved scan (buffers, triggers, sweep modes, ranges), so it works from the
instrument's own manual rather than from memory. Fetching a manual sends only its address to the maker's site."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from ._version import __version__

FOLDER = "instruments"
MAX_MANUAL = 80 * 1024 * 1024
PAGE = "\f"   # manual.txt keeps one page per form feed
Fetch = Callable[[str], bytes]


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Kelvin/%s (lab assistant)" % __version__})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(MAX_MANUAL + 1)
    if len(data) > MAX_MANUAL:
        raise ValueError("the manual is larger than %d MB" % (MAX_MANUAL // 2 ** 20))
    return data


def folder(home: Any, name: str) -> Path:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
        raise KeyError("no instrument %r" % name)
    return Path(home) / FOLDER / name


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def parse_idn(idn: str) -> Dict[str, str]:
    """Maker, model, serial number and firmware from an identity reply ("maker,model,serial,firmware" or colons)."""
    text = (idn or "").strip()
    if text.upper().startswith("IDN:"):
        text = text[4:]
    sep = "," if "," in text else ":"   # firmware versions may contain colons; SCPI replies use commas
    parts = [p.strip() for p in text.split(sep)]
    keys = ("maker", "model", "serial", "firmware")
    return {k: parts[i] for i, k in enumerate(keys) if i < len(parts) and parts[i]}


def write_profile(home: Any, name: str, card: Any, address: str, connection: Optional[Dict[str, Any]] = None,
                  idn: str = "", how: str = "", framing: str = "") -> Dict[str, Any]:
    """Create or update the instrument's instrument.json; what is already known (the manual) is kept."""
    path = folder(home, name) / "instrument.json"
    profile = _read_json(path)
    profile.update({"name": name, "card": card.id, "model": card.model, "vendor": card.vendor, "kind": card.kind,
                    "address": address, "card_manual": card.manual, "manual_url": getattr(card, "manual_url", "") or "",
                    "connection": dict(card.connection, **(connection or {})), "updated": time.time()})
    if idn:
        profile.update({"identity": idn, "parsed": parse_idn(idn), "identified_by": how or "identity reply"})
    if framing:   # the serial settings the instrument answered with
        profile["answered_with"] = framing
    profile.setdefault("found", time.time())
    _write_json(path, profile)
    return profile


def read_values(inst: Any) -> Dict[str, Any]:
    """Every readable setting and reading of one instrument (traces skipped). Reading never changes anything."""
    values: Dict[str, Any] = {}
    for spec in inst.card.params.values():
        if spec.type == "trace" or (spec.get_cmd is None and spec.name not in getattr(inst, "_last", {})):
            continue   # (a listen-only instrument's settings are what Kelvin last sent it)
        try:
            values[spec.name] = inst.read(spec.name)
        except Exception as e:
            values[spec.name] = "ERROR: %s" % (str(e).splitlines()[0] if str(e) else type(e).__name__)
    return values


def write_settings(home: Any, name: str, card: Any, values: Dict[str, Any]) -> Dict[str, Any]:
    """settings.json: every value read at setup, as the instrument reported it and as Kelvin shows it."""
    shown = {}
    for pname, v in values.items():
        spec = card.params.get(pname)
        if spec is None or isinstance(v, list):
            continue
        try:
            shown[pname] = {"value": v, "shown": spec.show(v), "kind": spec.kind, "unit": spec.unit}
        except Exception:
            shown[pname] = {"value": v, "kind": spec.kind, "unit": spec.unit}
    data = {"read_at": time.time(), "settings": shown}
    _write_json(folder(home, name) / "settings.json", data)
    return data


# ------------------------------------------------------------------ manuals

def pdf_pages(path: Path) -> List[str]:
    """The text of each page (needs pypdf, which comes with Kelvin's hardware support)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("reading PDF manuals needs pypdf: pip install pypdf")
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # font warnings about makers' PDFs are not the user's concern
    reader = PdfReader(str(path))
    if reader.is_encrypted:   # makers often lock printing or editing, not reading (decrypting needs cryptography)
        reader.decrypt("")
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return pages


def index_manual(home: Any, name: str) -> Dict[str, Any]:
    """Write manual.txt from manual.pdf (or manual.html) and note it in instrument.json."""
    base = folder(home, name)
    pdf, html = base / "manual.pdf", base / "manual.html"
    if pdf.exists():
        pages = pdf_pages(pdf)
        source = pdf
    elif html.exists():
        from .literature import html_to_text
        text = html_to_text(html.read_text(encoding="utf-8", errors="replace"))
        pages = [p for p in re.split(r"\n(?=## )", text) if p.strip()] or [text]
        source = html
    else:
        raise FileNotFoundError("no manual in %s" % base)
    if sum(len(p) for p in pages) < 2000:   # a sign-in page or a scanned PDF without text is no use to search
        raise ValueError("the maker's site sent a page with almost no text (it may want a sign-in), not the manual; "
                         "choose the PDF yourself" if source is html else
                         "the PDF has almost no text (a scan?), so Kelvin cannot search it")
    (base / "manual.txt").write_text(PAGE.join(p.replace(PAGE, " ") for p in pages), encoding="utf-8")
    profile = _read_json(base / "instrument.json")
    profile["manual"] = dict(profile.get("manual") or {}, file=source.name, pages=len(pages),
                             characters=sum(len(p) for p in pages), indexed=time.time())
    _write_json(base / "instrument.json", profile)
    return profile["manual"]


def fetch_manual(home: Any, name: str, card: Any, fetch: Fetch = http_get, force: bool = False) -> Dict[str, Any]:
    """Download the card's manual into the folder once, then index it. A manual put there by hand is used as it is."""
    base = folder(home, name)
    base.mkdir(parents=True, exist_ok=True)
    profile = _read_json(base / "instrument.json")
    url = getattr(card, "manual_url", "") or ""
    have = [p for p in (base / "manual.pdf", base / "manual.html") if p.exists()]
    if have and not force:
        if not (base / "manual.txt").exists() or (profile.get("manual") or {}).get("file") != have[0].name:
            return index_manual(home, name)
        return profile.get("manual") or {}
    if not url:
        raise LookupError("Kelvin has no address for the %s manual; put the PDF in %s as manual.pdf" % (card.model, base))
    data = fetch(url)
    is_pdf = data[:5] == b"%PDF-"
    if not is_pdf and b"<html" not in data[:4096].lower():
        raise ValueError("%s did not return a PDF or a web page" % url)
    return _store(home, name, card, data, "manual.pdf" if is_pdf else "manual.html", url=url)


def save_manual(home: Any, name: str, card: Any, data: bytes) -> Dict[str, Any]:
    """Use a PDF the person chose (for makers that keep their manuals behind a sign-in)."""
    if data[:5] != b"%PDF-":
        raise ValueError("that is not a PDF")
    return _store(home, name, card, data, "manual.pdf", url="")


def _store(home: Any, name: str, card: Any, data: bytes, file: str, url: str) -> Dict[str, Any]:
    base = folder(home, name)
    base.mkdir(parents=True, exist_ok=True)
    for old in ("manual.pdf", "manual.html", "manual.txt"):   # one manual per folder
        if old != file and (base / old).exists():
            (base / old).unlink()
    tmp = (base / file).with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, base / file)
    profile = _read_json(base / "instrument.json")
    profile["manual"] = {"url": url, "file": file, "bytes": len(data), "fetched": time.time(),
                         "title": card.manual or card.model, "added_by": "Kelvin" if url else "a person"}
    _write_json(base / "instrument.json", profile)
    try:
        return index_manual(home, name)
    except Exception:   # not kept: the next try fetches it again
        (base / file).unlink(missing_ok=True)
        profile.pop("manual", None)
        _write_json(base / "instrument.json", profile)
        raise


_WORD = re.compile(r"[A-Za-z0-9]+")   # a SCPI path such as SOURce:CURRent:LIMit counts as its words


def search_manual(home: Any, name: str, query: str, max_passages: int = 4, width: int = 900) -> List[Dict[str, Any]]:
    """The passages of the manual that best match the query, with their PDF page numbers. Rare words (a command
    mnemonic such as OFLT) count for more than common ones (time, set), and a passage must hold most of the words."""
    path = folder(home, name) / "manual.txt"
    if not path.exists():
        pdf = folder(home, name) / "manual.pdf"
        if pdf.exists() or (folder(home, name) / "manual.html").exists():
            index_manual(home, name)
        else:
            raise FileNotFoundError("no manual for %s yet" % name)
    pages = path.read_text(encoding="utf-8").split(PAGE)
    raw = [t for t in _WORD.findall(query or "") if len(t) > 1]
    if not raw:
        raise ValueError("say what to look for, e.g. 'buffer trigger sweep'")
    # a word in capitals (OFLT, SENS) or with digits is a mnemonic: only that word. Others also match longer words
    # that start with them (trigger: triggers, triggered; SOURce: source)
    exact = {t.lower() for t in raw if t.isupper() or any(c.isdigit() for c in t)}
    terms = sorted({t.lower() for t in raw})
    words = [re.compile(r"(?<![a-z0-9])%s%s" % (re.escape(t), r"(?![a-z0-9])" if t in exact else "")) for t in terms]
    lows = [p.lower() for p in pages]
    df = [sum(1 for low in lows if w.search(low)) for w in words]
    idf = [math.log((len(pages) + 1) / (d + 0.5)) for d in df]
    phrase = re.compile(r"[^a-z0-9]{0,3}".join(re.escape(t.lower()) for t in raw))
    scored = []
    step = width // 2
    for number, low in enumerate(lows, 1):
        if not any(w.search(low) for w in words):
            continue
        for start in range(0, max(1, len(low) - step), step):
            chunk = low[start:start + width]
            counts = [len(w.findall(chunk)) for w in words]
            have = sum(1 for c in counts if c)
            if not have:
                continue
            score = sum(f * (1 + math.log(c)) for f, c in zip(idf, counts) if c) * (have / len(terms)) ** 2
            if len(raw) > 1 and phrase.search(chunk):
                score *= 1.5
            scored.append((score, number, start))
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))
    out: List[Dict[str, Any]] = []
    shown: List[tuple] = []
    for score, number, start in scored:
        # begin the passage a little before the rarest word it holds, at a line or word break
        chunk = lows[number - 1][start:start + width]
        rarest = max((i for i, w in enumerate(words) if w.search(chunk)), key=lambda i: idf[i])
        at = start + words[rarest].search(chunk).start()
        page = pages[number - 1]
        begin = max(0, at - width // 4)
        brk = max(page.rfind("\n", begin - 120, at), page.rfind(". ", begin - 120, at))
        begin = brk + 1 if brk >= 0 and at - brk < width // 2 else page.find(" ", begin) + 1 if begin else 0
        if any(number == n and abs(begin - b) < width * 2 // 3 for n, b in shown):
            continue
        shown.append((number, begin))
        text = page[begin:begin + width].strip()
        out.append({"page": number, "text": re.sub(r"[ \t]+", " ", text)})
        if len(out) >= max_passages:
            break
    return out


def info(home: Any, name: str) -> Dict[str, Any]:
    base = folder(home, name)
    profile = _read_json(base / "instrument.json")
    settings = _read_json(base / "settings.json")
    profile["folder"] = str(base)
    profile["settings_read_at"] = settings.get("read_at")
    profile["has_manual"] = (base / "manual.txt").exists()
    return profile


def settings_of(home: Any, name: str) -> Dict[str, Any]:
    return _read_json(folder(home, name) / "settings.json")


def known(home: Any) -> List[str]:
    root = Path(home) / FOLDER
    return sorted(p.name for p in root.iterdir() if (p / "instrument.json").exists()) if root.exists() else []


def _first_line(e: BaseException) -> str:
    return str(e).splitlines()[0][:200] if str(e) else type(e).__name__


def prepare(home: Any, lab: Any, lock: Any, found: Optional[Dict[str, Dict[str, Any]]] = None,
            manuals: bool = True, fetch: Fetch = http_get, names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Give every real instrument of the lab its folder: profile, current settings, and (if allowed) its manual.
    `found` is what a scan learnt, by address; without it the instrument is asked who it is. `lock` keeps these reads
    from overlapping with anything else Kelvin does with the instruments."""
    report: Dict[str, Any] = {}
    for name, inst in lab.instruments.items():
        if inst.simulated or (names is not None and name not in names):
            continue
        card, address = inst.card, inst.config.address
        seen = (found or {}).get(address) or {}
        entry: Dict[str, Any] = {}
        try:
            with lock:
                idn, how = seen.get("idn") or "", seen.get("how") or ""
                if not idn and card.idn_pattern:
                    try:
                        idn, how = inst.query_raw(card.idn_query).strip(), "identity reply"
                    except Exception:
                        idn = ""
                values = read_values(inst)
            write_profile(home, name, card, address, getattr(inst.config, "connection", None), idn, how,
                          seen.get("settings") or "")
            write_settings(home, name, card, values)
            bad = [k for k, v in values.items() if isinstance(v, str) and v.startswith("ERROR")]
            entry["settings"] = "%d read" % (len(values) - len(bad)) + (", %d did not answer" % len(bad) if bad else "")
        except Exception as e:
            entry["error"] = _first_line(e)
        if manuals:
            try:
                m = fetch_manual(home, name, card, fetch)
                entry["manual"] = "%s pages" % m.get("pages", "?")
            except Exception as e:
                entry["manual"] = "not fetched: " + _first_line(e)
        report[name] = entry
    return report
