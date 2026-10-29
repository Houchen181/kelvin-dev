"""Each instrument's working folder: who it is, how Kelvin talks to it, its settings, and its manual.

    ~/Kelvin/instruments/<name>/
        instrument.json   identity (the *IDN? reply split into maker, model, serial number, firmware), card,
                          address, the connection settings that answered, when it was found, where the manual is from
        settings.json     every setting and reading at setup (ranges, time constants, sensitivities...), refreshed
                          each time Kelvin connects
        manual.pdf        the maker's manual, fetched once from the address on the card (or put there by hand)
        manual.txt        its text, page by page, which search_manual looks through
        calibration.json  every calibration a person recorded: when, due again, the certificate, the serial number
        certificates/     the certificates' files, when a person added them

Kelvin reads these when it plans an involved scan (buffers, triggers, sweep modes, ranges), so it works from the
instrument's own manual rather than from memory. Fetching a manual sends only its address to the maker's site."""
from __future__ import annotations

import datetime
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

def pdf_pages(path: Any) -> List[str]:
    """The text of each page of a PDF file (or of its bytes). Needs pypdf, which comes with Kelvin's hardware support."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("reading PDF manuals needs pypdf: pip install pypdf")
    import io
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # font warnings about makers' PDFs are not the user's concern
    reader = PdfReader(io.BytesIO(path) if isinstance(path, (bytes, bytearray)) else str(path))
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


def store_manual(home: Any, name: str, card: Any, data: bytes, url: str) -> Dict[str, Any]:
    """Keep a manual already downloaded (while Kelvin wrote the instrument's card)."""
    return _store(home, name, card, data, "manual.pdf" if data[:5] == b"%PDF-" else "manual.html", url=url)


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
        if inst.simulated or not inst.enabled or (names is not None and name not in names):
            continue   # switched off in the Lab panel: nothing is sent to it, not even who-are-you
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


# ------------------------------------------------------------------ calibration
# Each instrument's calibration as a person records it (in the portal: Lab, Set up…, Calibration… on its row): when it
# was calibrated, when it is due again, the certificate (its number, and its file when there is one) and the serial
# number it is for. health_check says when one is out of date or due soon, an approval card when one is out of date,
# and a run's ELN record which one was in force. Every record is kept, oldest first, in the instrument's folder.

CALIBRATION = "calibration.json"
CERTIFICATES = "certificates"
DUE_SOON_DAYS = 30
CERT_MAX = 20 * 1024 * 1024
CERT_KINDS = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def _date(value: Any, what: str) -> str:
    try:
        return datetime.date.fromisoformat(str(value or "").strip()[:10]).isoformat()
    except ValueError:
        raise ValueError("%s must be a date, YYYY-MM-DD." % what)


def calibrations(home: Any, name: str) -> List[Dict[str, Any]]:
    """Every calibration recorded for the instrument, oldest first."""
    data = _read_json(folder(home, name) / CALIBRATION)
    return [r for r in data.get("records") or [] if isinstance(r, dict) and r.get("due")]


def calibration(home: Any, name: str) -> Dict[str, Any]:
    """The calibration recorded last ({} while there is none)."""
    records = calibrations(home, name)
    return dict(records[-1]) if records else {}


def record_calibration(home: Any, name: str, due: Any, calibrated: Any = "", certificate: str = "", by: str = "",
                       serial: str = "", provider: str = "", note: str = "", file: str = "",
                       today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """A person's record of a calibration: ValueError for dates that can't be (due before it was calibrated, a
    calibration in the future). The serial number is the instrument's own (its identity) unless given."""
    today = today or datetime.date.today()
    record: Dict[str, Any] = {"due": _date(due, "The due date")}
    if str(calibrated or "").strip():
        record["calibrated"] = _date(calibrated, "The calibration date")
        if record["calibrated"] > today.isoformat():
            raise ValueError("The calibration date is in the future.")
        if record["calibrated"] > record["due"]:
            raise ValueError("It would be due again before it was calibrated: check the dates.")
    serial = str(serial or "").strip() or _serial(home, name)
    for key, value, limit in (("certificate", certificate, 200), ("serial", serial, 100), ("provider", provider, 200),
                              ("note", note, 500), ("file", file, 200), ("by", by, 200)):
        value = " ".join(str(value or "").split())[:limit]
        if value:
            record[key] = value
    record["recorded"] = datetime.datetime.now().isoformat(timespec="seconds")
    _write_json(folder(home, name) / CALIBRATION, {"records": calibrations(home, name) + [record]})
    return record


def save_certificate(home: Any, name: str, filename: str, data: bytes) -> str:
    """Keep a calibration certificate (a PDF, or a picture of it) in the instrument's folder; the name it is kept as."""
    ext = Path(str(filename or "")).suffix.lower()
    if ext not in CERT_KINDS:
        raise ValueError("A certificate must be a PDF, PNG or JPEG file.")
    if len(data) > CERT_MAX:
        raise ValueError("A certificate can be up to %d MB." % (CERT_MAX // 2 ** 20))
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        raise ValueError("That file isn't a PDF.")
    stem = re.sub(r"[^\w.-]+", "_", Path(filename).stem).strip("._")[:60] or "certificate"
    target = folder(home, name) / CERTIFICATES / ("%s-%s%s" % (datetime.date.today().isoformat(), stem, ext))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target.name


def certificate_path(home: Any, name: str, file: str) -> Path:
    """A kept certificate, by the name its record gives; KeyError for anything else."""
    base = folder(home, name) / CERTIFICATES
    if not re.fullmatch(r"[\w.-]+", str(file or "")) or str(file).startswith(".") or not (base / file).is_file():
        raise KeyError("no certificate %r for %s" % (file, name))
    return base / file


def calibration_state(record: Dict[str, Any], today: Optional[datetime.date] = None,
                      serial_now: str = "") -> Dict[str, Any]:
    """Where a calibration stands: state none, ok, soon (due within DUE_SOON_DAYS) or overdue, with the days to go
    (negative: past), in words; "serial" when the instrument reports another serial number than the record's."""
    if not record or not record.get("due"):
        return {"state": "none", "words": "no calibration on record"}
    today = today or datetime.date.today()
    days = (datetime.date.fromisoformat(record["due"]) - today).days
    cert = " (certificate %s)" % record["certificate"] if record.get("certificate") else ""
    if days < 0:
        out = {"state": "overdue", "days": days, "words": "out of date since %s%s" % (record["due"], cert)}
    elif days <= DUE_SOON_DAYS:
        out = {"state": "soon", "days": days, "words": "due %s, in %d day%s%s" % (
            record["due"], days, "" if days == 1 else "s", cert)}
    else:
        out = {"state": "ok", "days": days, "words": "%sdue %s%s" % (
            "calibrated %s, " % record["calibrated"] if record.get("calibrated") else "", record["due"], cert)}
    if serial_now and record.get("serial") and serial_now.strip().lower() != str(record["serial"]).strip().lower():
        out["serial"] = "the calibration on record is for serial %s, but it reports serial %s" % (
            record["serial"], serial_now)
    return out


def _serial(home: Any, name: str) -> str:
    """The serial number the instrument gave in its identity reply when Kelvin last connected ("" unknown)."""
    return str((info(home, name).get("parsed") or {}).get("serial") or "")


def calibration_notes(home: Any, names: Iterable[str], today: Optional[datetime.date] = None) -> List[str]:
    """For health_check: each instrument whose calibration is out of date or due soon, or on record for another
    serial number. A note, never a failure: the instrument works, and its readings say what they say."""
    out = []
    for name in names:
        st = calibration_state(calibration(home, name), today, _serial(home, name))
        if st["state"] == "overdue":
            out.append("%s calibration %s: its readings may have drifted; recalibrate it, or record the new "
                       "calibration (Lab, Set up…, Calibration…)" % (name, st["words"]))
        elif st["state"] == "soon":
            out.append("%s calibration %s" % (name, st["words"]))
        if st.get("serial"):
            out.append("%s: %s" % (name, st["serial"]))
    return out


def card_lines(home: Any, names: Iterable[str], today: Optional[datetime.date] = None) -> List[str]:
    """For an approval card: the instruments it names whose calibration is out of date, or on record for another
    serial number (cardlang.py translates these lines)."""
    out = []
    for name in names:
        record = calibration(home, name)
        st = calibration_state(record, today, _serial(home, name))
        if st["state"] == "overdue":
            out.append("Calibration out of date: %s was due %s%s" % (
                name, record["due"], " (certificate %s)" % record["certificate"] if record.get("certificate") else ""))
        if st.get("serial"):
            out.append("Calibration on record is for serial %s; %s reports serial %s" % (
                record["serial"], name, _serial(home, name)))
    return out


def calibration_at(home: Any, name: str, when: datetime.datetime) -> Dict[str, Any]:
    """The calibration in force at a moment (a run's start): the last one recorded by then, with where it stood that
    day ({} when none was)."""
    stamp = when.isoformat(timespec="seconds")
    then = [r for r in calibrations(home, name) if str(r.get("recorded") or "") <= stamp]
    return dict(then[-1], state=calibration_state(then[-1], when.date())) if then else {}


def calibration_view(home: Any, name: str, today: Optional[datetime.date] = None) -> Dict[str, Any]:
    """For the portal: the latest record and where it stands, and how many were recorded before it."""
    records = calibrations(home, name)
    last = records[-1] if records else {}
    return {"record": last, "earlier": max(0, len(records) - 1),
            "state": calibration_state(last, today, _serial(home, name))}
