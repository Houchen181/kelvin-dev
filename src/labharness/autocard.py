"""Kelvin writes the card for an instrument it does not know: it finds the maker's manual, drafts the card from the
manual's command reference with your model, and makes it safe to try whatever the model wrote.

    1. The manual. Addresses your model suggests on the maker's own site, and known patterns for some makers. A web
       page is followed to the links that look most like this model's manual (naming the model or its series,
       PDFs, "programming" or "manual"): pages on the maker's site, and PDFs the page links as manuals (also on the
       maker's download host), up to three links deep through pages about the model (product page, list of
       manuals, manual page, PDF). A document counts only once Kelvin has downloaded it and its text names the
       model or its series (the AFG31000 manual for an AFG31252); of those, the one with the most command syntax
       wins. A person can also give the manual: a PDF on this computer or its web address.
    2. The card. The model writes it from the command reference (the pages densest in commands), the card checker
       reviews it, and the model fixes what the checker found, once. What the model left out or is unsure of goes
       to the person (and into the card's header). Where the manual leaves an output's ranges to another document
       (Tektronix's programmer manuals send them to a specifications document), the model says which; Kelvin looks
       for that document the same way and has the card written again with its pages, so the output gets its
       limits. A person can also give it.
    3. Safe to try. Status draft: on real hardware every setting and output change asks a person, saying the card
       is a draft (gate.py). No commands are sent on connect. Reads that start something (MEASure?, READ?, INITiate,
       *TST?, *CAL?: on some instruments they switch an output on or run a self-test) are left out. The identity
       pattern is checked against the instrument's own reply; for an instrument that gave none (the person named
       it), the manual's identity check stays as written, for try_card to test.

Only the maker and the model name leave the computer (to your model and the maker's site), never the serial number.
A person reviews the card against the manual and marks it reviewed; until then nothing on it changes without them."""
from __future__ import annotations

import html as html_lib
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urljoin, urlparse

from . import carddraft, profiles
from .scpi import is_query

Ask = Callable[[str, str], Tuple[str, str]]       # (system, prompt) -> (answer, model key)
Fetch = Callable[[str], bytes]

MAX_FETCHES = 16
MAX_DEPTH = 3      # links followed from a suggested page: product page -> manuals list -> manual page -> PDF
GOOD = 40          # command-syntax hits that make a document a command reference
MANUAL_WORDS = re.compile(r"manual|programm(?:ing|er)|command reference|reference (?:manual|guide)|user'?s? guide", re.I)
PROGRAMMING_WORDS = re.compile(r"programm(?:ing|er)|command|remote|scpi", re.I)   # the manual a card is written from
# the document an instrument's ranges are in: "AFG31000_Spec_PV", a datasheet, a technical reference
SPEC_WORDS = re.compile(r"(?<![a-z])spec(?:ification)?s?(?![a-z])|data ?sheet|technical reference|performance "
                        r"verification", re.I)
# reads that do something: measure (and on some source-meters switch the output on), trigger, self-test, calibrate
ACTION_READ = re.compile(r"^[\s:]*(MEAS(?:ure)?|READ|INIT(?:iate)?|\*CAL|\*TST|\*TRG|TRIG(?:ger)?)\b", re.I)
# a measurement read kept when the card reads it only while its output is already on (read_when): it cannot switch
# that output on (Kelvin's own Keithley 2400 card reads :READ? so); self-tests, calibration and triggers never
MEASURE_READ = re.compile(r"^[\s:]*(MEAS(?:ure)?|READ)\b", re.I)
_CMD = re.compile(r"(?<![A-Za-z0-9])(\*[A-Z]{3}\??"                                     # *IDN?
                  r"|\[?:?[A-Z]{3,}[a-z]*\d?\]?(?:\[?:[A-Z]{2,}[a-z]*\d?\]?)+\??"   # SOURce:VOLTage, OUTPut1:STATe
                  r"|:[A-Z]{3,}[a-z]*\d?\??"                                        # :APPLy, :VOLT 7.5
                  r"|[A-Z]{3,5}\?)")                                                # KRDG?
# a command named by its label or its heading, as Lake Shore's are: "Input SETP <output>,<value>[term]",
# "SETP Control Setpoint Command"
_NAMED = re.compile(r"^\s*(?:(?:Input|Syntax|Command)\s*:?\s+([A-Z][A-Z0-9]{2,}\??)(?=[\s<\[(,]|$)"
                    r"|([A-Z][A-Z0-9]{2,}\??)\s+[A-Z][A-Za-z /&-]*\s(?:Command|Query)\s*$)")
# a channel number or an index in the syntax, read as the bare command: SOURce[<n>], OUTPut[1|2], SOURce<3|4>,
# SEQuence:ELEM[n]
_SUFFIX = re.compile(r"\[<\w+>\]|<n>|\[\d(?:\|\d)*\]|<\d(?:\|\d)*>|\[[a-z]\]")
# a command written with its line end, as serial protocols are: "PR1 <CR>", "SEN [,a,b] <CR>[<LF>]" (Pfeiffer)
_TERMINATED = re.compile(r"(?<![A-Za-z0-9<])[A-Z]{2,6}[a-z0-9]?\??[^\n<>]{0,40}<CR>")
# a table of contents or index line: dot leaders, a page number like 2-90, "(on page 149)" (Tektronix)
_CONTENTS = re.compile(r"\.{4,}|\s\d+-\d+\s*$|\(on\s+page\s+\d+\)?|^\s*page\s+\d+\)", re.I)
KNOWN = [   # makers whose manuals sit at a predictable address
    (re.compile(r"stanford|\bsrs\b", re.I), "https://www.thinksrs.com/downloads/pdfs/manuals/{MODEL}m.pdf"),
]
SUGGEST_SYSTEM = ("You help a lab assistant find instrument manuals. Answer with web addresses only, one per line, "
                  "no other text.")


def maker_model(idn: str) -> Tuple[str, str]:
    p = profiles.parse_idn(idn)
    return p.get("maker", "").strip(), p.get("model", "").strip()


def suggest_prompt(maker: str, model: str, document: str = "") -> str:
    if document:   # the document a card's ranges are in, as the manual names it
        return ("Where can the %s %s document %r be downloaded? It has the instrument's specifications, such as its "
                "output ranges; ideally a PDF. Give up to 6 addresses on %s's own website, the most likely first; a "
                "product or downloads page that links it is fine too. Only addresses you are confident exist."
                % (maker, model, document, maker))
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
    text = _SUFFIX.sub("", text or "")
    return len(_CMD.findall(text)) + len(_TERMINATED.findall(text))


_DEFINES = re.compile(r"^\s*(Syntax|Examples?|Input|Returned|Format|Query|Command|Transmit|Receive|Arguments)\b", re.I)
# what a card is made of: outputs, levels, ranges, readings; and what it seldom needs: the interface, the display,
# stored setups, triggering, status registers. Told by a command's first node: OUTPut:STATe is an output.
_CORE = re.compile(r"VOLT|CURR|OUTP|APPL|SOUR|MEAS|READ|FETC|CONF|FREQ|AMPL|FUNC|POW|LEV|RANG|PHAS|TEMP|SETP|FIEL|"
                   r"SENS|INST|RDG|HTR|MOUT|STILL|ARM|WAVE", re.I)
_SIDE = re.compile(r"SYST|COMM|LAN|RS232|GPIB|MMEM|DISP|TIME|DELAY|REC|ANAL|PRES|TRIG|STAT|BEEP|LANG|BRIG|CONT|LIC|"
                   r"MEM|CAL|KLOC|SAV|RCL|MONI|NET|IEEE|BAUD|ERR|\*", re.I)


def _nodes(command: str) -> List[str]:
    """The nodes a command must be sent with, in short form: [:SOURce]:VOLTage[:LEVel] is VOLT, OUTPut1:STATe is
    OUTP, STAT; a leading SOURce or SENSe is dropped, as it is optional in SCPI."""
    required = re.sub(r"\[[^\[\]]*\]", "", re.sub(r"\[[^\[\]]*\]", "", command))
    nodes = [re.sub(r"\d+$", "", re.match(r"[*A-Z]*", part).group(0)) for part in required.strip(":?").split(":")]
    nodes = [n for n in nodes if n]
    if len(nodes) > 1 and nodes[0] in ("SOUR", "SOURCE", "SENS", "SENSE"):
        nodes = nodes[1:]
    return nodes or [command]


def _weight(command: str) -> int:
    """Outputs, levels and readings count most, the interface and housekeeping least; a command of one node (OUTPut,
    VOLTage, SETP) twice as much as one of two, and four times as much as a deeper one (AM:INTernal:FUNCtion)."""
    nodes = _nodes(command)
    weight = 1 if _SIDE.match(nodes[0]) else 3 if _CORE.search(nodes[0]) else 2
    return weight * {1: 4, 2: 2}.get(len(nodes), 1)


def detail_score(text: str) -> int:
    """Commands on lines that define or explain them (syntax, a description, an example), or anywhere on a page that
    defines commands (a Syntax heading), but not in a table of contents or an index; weighed by what they are. The
    pages a card is best written from."""
    lines = _SUFFIX.sub("", text or "").splitlines()
    defines = any(_DEFINES.match(line) and not _CONTENTS.search(line) for line in lines)
    n = 0
    for line in lines:
        if _CONTENTS.search(line):
            continue
        found = _CMD.findall(line) + _TERMINATED.findall(line)
        named = _NAMED.match(line)
        name = named and (named.group(1) or named.group(2))
        if name and name not in found:
            found.append(name)
        if not found:
            continue
        rest = re.sub(r"\{[^}]*\}|<[^>]*>|\[[^\]]*\]", " ", _TERMINATED.sub(" ", _CMD.sub(" ", line)))   # arguments
        if defines or name or len(re.findall(r"[A-Za-z]{2,}", rest)) >= 2:
            n += sum(_weight(c) for c in found)
    return n


_VALUE = re.compile(r"\d(?:\.\d+)?\s?[munµkMG]?(?:V|A|Hz|W|K|s|Ohm|Ω)(?![A-Za-z])")   # 30V, 3.2 A, 100 kHz, 4 K


def spec_score(text: str, model: str) -> int:
    """Values with units on a page that names this model itself: its ranges and specifications (the DP832's
    "CH1 (30V/3A) 0V to 32V/0A to 3.2A"), which a card's limits come from."""
    low = (text or "").lower()
    words = _model_words(model)
    if not words or not any(w in low for w in words):
        return 0
    return len(_VALUE.findall(text))


# what an interface page names: how to talk to the instrument at all (its serial framing, line ends, handshake)
_INTERFACE = [re.compile(t, re.I) for t in (r"baud", r"parity", r"data ?bits?", r"stop ?bits?", r"terminat(?:ion|or)",
                                            r"handshake|flow control", r"<ACK>|\bACK\b", r"<ENQ>|\bENQ\b",
                                            r"<CR>|carriage return", r"<LF>|line ?feed", r"RS-?232", r"RS-?485")]


def interface_score(text: str) -> int:
    """How many interface settings a page names (baud rate, parity, line ends, ACK and ENQ, RS-232...)."""
    return sum(bool(t.search(text or "")) for t in _INTERFACE)


def specs_score(pages: List[str], model: str) -> int:
    """How much of this model's specifications a document holds: the values on its three richest pages."""
    return sum(sorted((spec_score(p, model) for p in pages), reverse=True)[:3])


_GENERIC = re.compile(r"^(?:model|mod|type|series|no)(?=\d)")   # a maker's MODEL372 is the Model 372
_SERIES = re.compile(r"(?<![a-z0-9])([a-z]*)([1-9]\d*?)(0{2,}|x+)(?![a-z0-9])")   # AFG31000, DP800, 622x, 6200


def _model_words(model: str) -> List[str]:
    """The words of a model name that carry digits, as manuals write them: 6221, afg31252, dp832, 372 (MODEL372)."""
    out = []
    for w in re.findall(r"[a-z0-9]+", model.lower()):
        w = _GENERIC.sub("", w)
        if len(w) >= 3 and any(c.isdigit() for c in w):
            out.append(w)
    return out


def _series_named(word: str, low: str) -> bool:
    """Does the text name the series of this model word: AFG31000 for afg31252, DP800 for dp832, 622x or Series 6200
    for 6221? A series name ends in two or more zeros or in x; SR860 is another model, not the SR865A's series."""
    m = re.fullmatch(r"([a-z]*)(\d+)([a-z0-9]*)", word)
    if not m:
        return False
    letters, digits = m.group(1), m.group(2)
    for s in _SERIES.finditer(low):
        if s.group(1) == letters and digits.startswith(s.group(2)) and (letters or len(s.group(2)) >= 2):
            return True
    return False


def names_model(text: str, model: str) -> bool:
    """Does the document name this model, or its series (a series manual is the model's manual)? Every word of the
    model's name that has a digit must appear, or its series; a name without digits needs its longest word."""
    low = (text or "").lower()
    words = _model_words(model)
    if not words:
        letters = [w for w in re.findall(r"[a-z]+", model.lower()) if len(w) >= 3]
        return bool(letters) and max(letters, key=len) in low
    return all(w in low or _series_named(w, low) for w in words)


def _site(url: str) -> str:
    host = urlparse(url).hostname or ""
    return ".".join(host.split(".")[-2:])


def _same_page(a: str, b: str) -> bool:
    """The same page, or the same page in another language (/en/..., /fr/..., /zh-cn/...)."""
    def path(u: str) -> str:
        p = urlparse(u)
        return (p.hostname or "") + re.sub(r"^/[a-z]{2}(?:[-_][a-z]{2})?(?=/)", "", p.path.rstrip("/"), flags=re.I)
    return path(a) == path(b)


def _clean(url: str) -> str:
    """An address as a browser would send it: spaces and other unsafe characters percent-encoded."""
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def page_links(html: str, base: str) -> List[Tuple[str, str]]:
    """The links on a web page as (label, address), each once: documents (PDFs) first, then the rest."""
    docs: List[Tuple[str, str]] = []
    rest: List[Tuple[str, str]] = []
    for href, label in re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, re.I | re.S):
        url = _clean(urljoin(base, html_lib.unescape(href.strip())))
        if not url.startswith(("https://", "http://")) or _same_page(url, base) or any(u == url for _, u in docs + rest):
            continue
        text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", label))).strip()[:100]
        (docs if url.lower().split("?")[0].split("#")[0].endswith(".pdf") else rest).append((text, url))
    return docs + rest


def pdf_links(html: str, base: str, model: str = "", want: Any = PROGRAMMING_WORDS) -> List[str]:
    """Links on a maker's page that may lead to the model's manual, best first: those naming the model (or its
    series), PDFs, and links that say programming (want: what else to look for) or manual. Pages are followed on the
    maker's site only; a PDF the page links as a manual or guide may be on the maker's download host. The site's
    general pages are left out."""
    words = _model_words(model)
    scored: List[Tuple[int, int, str]] = []
    for n, (href, label) in enumerate(re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, re.I | re.S)):
        url = _clean(urljoin(base, html_lib.unescape(href.strip())))
        about = (unquote(url) + " " + html_lib.unescape(re.sub(r"<[^>]+>", " ", label))).lower()
        if not url.startswith(("https://", "http://")) or _same_page(url, base):
            continue
        is_pdf = url.lower().split("?")[0].split("#")[0].endswith(".pdf")
        says = 2 if want.search(about) else 1 if MANUAL_WORDS.search(about) else 0
        if _site(url) != _site(base) and not (is_pdf and says):
            continue   # another site: only a manual the maker's page links (e.g. Rigol's download host)
        names = 4 if any(w in about for w in words) else 2 if any(_series_named(w, about) for w in words) else 0
        if not (is_pdf or says) or (words and not is_pdf and not names):
            continue   # e.g. the site's "Datasheets and Manuals" page, or another product
        if url not in [u for _, _, u in scored]:
            scored.append((-(names + 2 * is_pdf + says), n, url))
    return [u for _, _, u in sorted(scored)][:6]


def find_manual(maker: str, model: str, ask: Optional[Ask], fetch: Fetch,
                log: Callable[[str], None] = lambda m: None,
                problems: Optional[List[str]] = None, document: str = "",
                exclude: Any = (), search: Optional[Callable[[str], List[str]]] = None) -> Optional[Dict[str, Any]]:
    """The best manual Kelvin could download and check for this model, or None (and why, in `problems`). document:
    the one its ranges are in instead (a specifications document the manual names); the best has the most of this
    model's values. exclude: addresses not to fetch (the manual itself). search: a web search (query -> addresses),
    whose finds are tried before the addresses the model remembers."""
    queue: List[Tuple[str, int]] = []
    compact = re.sub(r"[^A-Za-z0-9]", "", model).upper()
    for rx, pattern in [] if document else KNOWN:
        if rx.search(maker):
            queue.append((pattern.format(MODEL=compact), 0))
    if search is not None:
        query = "%s %s %s pdf" % (maker, model, document or "programming manual")
        try:
            queue += [(u, 0) for u in search(query)[:5]]
        except Exception as e:
            log("web search for %r: %s" % (query, profiles._first_line(e)))
    if ask is not None:
        try:
            answer, _ = ask(SUGGEST_SYSTEM, suggest_prompt(maker, model, document))
            queue += [(u, 0) for u in urls_in(answer)[:6]]
        except Exception as e:
            log("could not ask the model for the manual's address: %s" % profiles._first_line(e))
            if problems is not None:
                problems.append(profiles._first_line(e))
    best: Optional[Dict[str, Any]] = None
    seen, fetches = set(exclude), 0
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
            from .literature import html_to_text
            pages = [html_to_text(html)]
            kind = "html"
            # a product or downloads page: what it links; a page about this model (or its series): its manuals'
            # pages and PDFs, as on makers' support sites (product -> manuals list -> manual page -> PDF)
            if depth == 0 or (depth < MAX_DEPTH and names_model(pages[0], model)):
                links = pdf_links(html, url, model, SPEC_WORDS if document else PROGRAMMING_WORDS)
                if depth == MAX_DEPTH - 1:
                    links = [u for u in links if u.lower().split("?")[0].endswith(".pdf")]
                queue = [(u, depth + 1) for u in links[:6 if depth == 0 else 4]] + queue
        else:
            continue
        head = "\n".join(pages[:20])
        if not names_model(head, model):
            log("%s: does not name the %s" % (url, model))
            continue
        score = specs_score(pages, model) if document else sum(command_score(p) for p in pages)
        if kind == "html" and (sum(len(p) for p in pages) < 5000 or score < 15):
            continue   # a web page counts only if it is a command reference (or the specifications) itself
        found = {"url": url, "pages": pages, "score": score, "kind": kind, "data": data}
        if best is None or score > best["score"]:
            best = found
        if score >= GOOD and kind == "pdf":
            break
    return best


def manual_from(source: str, fetch: Fetch) -> Dict[str, Any]:
    """A manual the person gave: a PDF, web page or text file on this computer, or its web address."""
    if re.match(r"^https?://", source, re.I):
        data, where = fetch(_clean(source)), source
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            raise ValueError("there is no file %s" % source)
        if path.stat().st_size > profiles.MAX_MANUAL:
            raise ValueError("it is larger than %d MB" % (profiles.MAX_MANUAL // 2 ** 20))
        data, where = path.read_bytes(), str(path)
    if data[:5] == b"%PDF-":
        pages, kind = profiles.pdf_pages(data), "pdf"
    elif b"<html" in data[:4096].lower():
        from .literature import html_to_text
        pages, kind = [html_to_text(data.decode("utf-8", errors="replace"))], "html"
    else:
        pages, kind = [data.decode("utf-8", errors="replace")], "text"
    return {"url": where, "pages": pages, "score": sum(command_score(p) for p in pages), "kind": kind, "data": data}


def command_excerpt(pages: List[str], limit: int = carddraft.MAX_MANUAL, model: str = "") -> str:
    """The pages a card is written from, in their own order, up to `limit` characters, each marked with its page:
    first up to three pages with this model's own values (its ranges) and up to three that say how to talk to it
    (its serial settings, line ends, a handshake like Pfeiffer's ACK and ENQ), then those that explain the most
    commands (syntax, descriptions, examples; not the contents or the index)."""
    score = detail_score if any(detail_score(p) for p in pages) else command_score
    scores = [score(p) for p in pages]
    specs = [spec_score(p, model) for p in pages]
    first = sorted((i for i in range(len(pages)) if specs[i] >= 4), key=lambda i: -specs[i])[:3]
    talk = [interface_score(p) for p in pages]
    first += sorted((i for i in range(len(pages)) if talk[i] >= 4 and i not in first), key=lambda i: (-talk[i], i))[:3]
    ranked = first + sorted((i for i in range(len(pages)) if scores[i] > 0 and i not in first), key=lambda i: -scores[i])
    chosen, size = [], 0
    for i in ranked:
        text = pages[i].strip()
        if size + len(text) > limit:
            continue
        chosen.append(i)
        size += len(text) + 12
    return "\n\n".join("[page %d]\n%s" % (i + 1, pages[i].strip()) for i in sorted(chosen))


SERIAL_KEYS = ("baud_rate", "data_bits", "parity", "stop_bits", "read_termination", "write_termination")


def framing(serial: Dict[str, Any]) -> str:
    """Serial settings in a person's words: 57600 7O1."""
    return "%s %s%s%g" % (serial.get("baud_rate"), serial.get("data_bits", 8), str(serial.get("parity", "none"))[0].upper(),
                          float(serial.get("stop_bits", 1)))


def make_safe(text: str, maker: str, model: str, idn: str, url: str, model_key: str,
              serial: Optional[Dict[str, Any]] = None, notes: Optional[List[str]] = None,
              specs: str = "") -> Tuple[str, List[str]]:
    """The card as Kelvin keeps it, whatever the model wrote (see the module's notes), and what was changed.
    serial: the settings a serial instrument answered the scan with, for a card whose manual pages did not say.
    notes: what the model left out or is unsure of, and specs: where the ranges came from, for the card's header."""
    import yaml
    data = yaml.safe_load(carddraft.extract_yaml(text))
    if not isinstance(data, dict):
        raise ValueError("the model did not answer with a card")
    changed: List[str] = []
    data["status"] = "draft"
    if data.pop("on_connect", None):
        changed.append("left out the commands on connect")
    conn = data.get("connection") if isinstance(data.get("connection"), dict) else {}
    if serial and serial.get("baud_rate") and "baud_rate" not in conn:
        conn.update({k: serial[k] for k in SERIAL_KEYS if k in serial and k not in conn})
        data["connection"] = conn
        changed.append("set the serial settings it answered the scan with (%s)" % framing(serial))
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
        guard = params.get(str(p.get("read_when") or ""))
        guarded = isinstance(guard, dict) and guard.get("kind") == "drive" and guard.get("type") == "bool"
        if get and (not is_query(get, conn.get("enq") is True)
                    or (ACTION_READ.match(get) and not (guarded and MEASURE_READ.match(get)))):
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
    head = ("# Drafted by Kelvin on %s from %s%s with %s.\n# A draft: check every command, limit and safe value "
            "against the manual, then mark it reviewed in Lab setup.\n"
            % (time.strftime("%Y-%m-%d"), url, " (ranges from %s)" % specs if specs else "", model_key))
    if notes:
        head += "# What the model left out or was unsure of:\n" + "".join("#   %s\n" % n for n in notes)
    return head + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110), changed


def ranges_named(notes: List[str]) -> Tuple[str, List[str]]:
    """The document the model said some outputs' ranges are in, and which outputs ("Ranges in: <title>; for:
    amplitude, offset"), or ('', [])."""
    for note in notes or []:
        m = re.search(r"\branges in:\s*([^;]+?)\s*(?:;\s*for:?\s*(.*))?$", note, re.I)
        if m and m.group(1).strip(" .\"'"):
            about = [w.strip(" .\"'") for w in re.split(r",|\band\b|/", m.group(2) or "")]
            return m.group(1).strip(" .\"'"), [w for w in about if w]
    return "", []


def specs_excerpt(pages: List[str], model: str, about: List[str], limit: int = 25000) -> str:
    """The pages of a specifications document a card's ranges come from, up to `limit` characters, in their own
    order: first those with a table on the outputs the model asked about (about: amplitude, offset; "Table 9:
    Amplitude specifications"), then those naming them, then those with the most values. A long page (a web
    datasheet) is taken in parts."""
    words = [w.lower() for w in about]
    parts: List[Tuple[str, str]] = []
    for i, page in enumerate(pages):
        text, chunks = page.strip(), []
        while len(text) > 4000:   # at a line end near 3000 characters
            cut = text.rfind("\n", 2000, 3000)
            cut = cut if cut > 0 else 3000
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        chunks.append(text)
        parts += [("%d" % (i + 1) if len(chunks) == 1 else "%d, part %d" % (i + 1, k + 1), c)
                  for k, c in enumerate(chunks)]

    def rank(text: str) -> Tuple[int, int, int]:
        titles = " ".join(re.findall(r"Table \d+[:.][^\n]*", text)).lower()
        return (sum(w in titles for w in words), sum(w in text.lower() for w in words), len(_VALUE.findall(text)))
    ranks = [rank(text) for _, text in parts]
    chosen, size = [], 0
    for n in sorted((n for n in range(len(parts)) if ranks[n][0] or ranks[n][2] >= 4), key=lambda n: ranks[n],
                    reverse=True):
        if size + len(parts[n][1]) <= limit:
            chosen.append(n)
            size += len(parts[n][1]) + 40
    return "\n\n".join("[specifications, page %s]\n%s" % parts[n] for n in sorted(chosen))


def draft(manual_pages: List[str], maker: str, model: str, idn: str, url: str, ask: Ask,
          serial: Optional[Dict[str, Any]] = None,
          specs: Optional[Dict[str, Any]] = None) -> Tuple[str, List[str], str, List[str]]:
    """Write the card with the model, check it, and have the model repair it once. specs: the document with the
    instrument's ranges ({url, pages, about}), whose pages go first. Returns (card text, changes, model, what the
    model left out or is unsure of)."""
    name = ("%s %s (it answers *IDN? with %r)" % (maker, model, ",".join(idn.split(",")[:2])) if idn else
            "%s %s (as the person named it; Kelvin has no identity reply from it)" % (maker, model))
    if serial and serial.get("baud_rate"):
        name += "; over serial it answered at %s" % framing(serial)
    ranges = specs_excerpt(specs["pages"], model, specs.get("about") or []) if specs else ""
    answer, key = ask(carddraft.DRAFT_SYSTEM, carddraft.draft_prompt(command_excerpt(manual_pages, model=model), name,
                                                                      ranges))
    notes = carddraft.notes_in(answer)
    where = (specs or {}).get("url", "")
    text, changed = make_safe(answer, maker, model, idn, url, key, serial, notes, where)
    card, problems, _ = carddraft.check(text)
    if problems:
        answer, key = ask(carddraft.DRAFT_SYSTEM, carddraft.repair_prompt(text, problems))
        notes = carddraft.notes_in(answer) or notes
        text, changed = make_safe(answer, maker, model, idn, url, key, serial, notes, where)
        card, problems, _ = carddraft.check(text)
    if problems:
        raise ValueError("the card the model wrote still has problems: " + "; ".join(problems[:4]))
    return text, changed, key, notes


def make_card(found: Dict[str, Any], cards_dir: Any, ask: Optional[Ask], fetch: Optional[Fetch] = None,
              builtin_ids: Any = (), log: Callable[[str], None] = lambda m: None,
              search: Optional[Callable[[str], List[str]]] = None) -> Dict[str, Any]:
    """For an instrument that matches no card: find its manual (or read the one the person gave: found's manual, a
    file or a web address), write its card, keep it as a draft. Maker and model come from its identity reply, else
    from found's maker and model (what the person said it is). Where the model says the manual leaves some ranges to
    another document, Kelvin finds it (or reads found's specs, which the person gave) and writes the card again.
    Returns {card_id, model, kind, manual: {url, data, kind}, changed, parameters, notes, specs, ranges_in} (specs:
    where the ranges came from; ranges_in: the document they are in, if Kelvin could not get it) or {error}."""
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
    given = str(found.get("manual") or "").strip()
    if given:   # the person gave it: a file on this computer or its web address
        try:
            manual = manual_from(given, fetch)
        except Exception as e:
            return {"error": "Kelvin could not read the manual %s: %s" % (given, profiles._first_line(e))}
        if not names_model("\n".join(manual["pages"][:20]), model):
            return {"error": "%s does not name the %s %s: is it its manual?" % (given, maker, model)}
    else:
        asked: List[str] = []
        manual = find_manual(maker, model, ask, fetch, log, asked, search=search)
        if manual is None and asked:
            return {"error": "Kelvin could not ask a model about the %s %s (%s)" % (maker, model, asked[0])}
        if manual is None:
            return {"error": "Kelvin could not find the %s %s manual online: choose its PDF under Make a card for "
                             "it from its manual" % (maker, model)}
    if manual["score"] < 15:
        return {"error": "the %s %s manual (%s) has no command reference: choose the programming manual's PDF in "
                         "Lab setup" % (maker, model, manual["url"])}
    specs: Optional[Dict[str, Any]] = None
    if found.get("specs"):   # the person gave the document with its ranges
        try:
            specs = manual_from(str(found["specs"]).strip(), fetch)
        except Exception as e:
            return {"error": "Kelvin could not read %s: %s" % (found["specs"], profiles._first_line(e))}
        if not names_model("\n".join(specs["pages"]), model):
            return {"error": "%s does not name the %s %s: are they its specifications?" % (found["specs"], maker, model)}
    serial = found.get("serial")
    try:
        text, changed, key, notes = draft(manual["pages"], maker, model, idn, manual["url"], ask, serial)
    except Exception as e:
        return {"error": "Kelvin could not write the %s %s card: %s" % (maker, model, profiles._first_line(e))}
    ranges_in, about = ranges_named(notes)
    if ranges_in and specs is None:   # the manual leaves some ranges to another document: find it
        specs = find_manual(maker, model, ask, fetch, log, document=ranges_in, exclude=[manual["url"]],
                            search=search)
        if specs is not None:
            log("%s: %s" % (ranges_in, specs["url"]))
    if specs is not None:   # and write the card again with its pages on those outputs
        specs["about"] = about
        try:
            text, changed, key, notes = draft(manual["pages"], maker, model, idn, manual["url"], ask, serial, specs)
            ranges_in, _ = ranges_named(notes)
        except Exception as e:
            log("could not write the card again with %s: %s" % (specs["url"], profiles._first_line(e)))
            specs = None
    try:
        card, path, _ = carddraft.save(cards_dir, text, builtin_ids, by="kelvin")
    except Exception as e:
        return {"error": "Kelvin could not write the %s %s card: %s" % (maker, model, profiles._first_line(e))}
    log("wrote %s from %s with %s (%s)" % (card.id, manual["url"], key, "; ".join(changed) or "as drafted"))
    params = ", ".join("%s (%s%s)" % (p.name, "output" if p.is_drive else "setting" if p.set_cmd else "reading",
                                      ", " + p.unit if p.unit else "") for p in card.params.values())
    return {"card_id": card.id, "model": card.model, "kind": card.kind, "changed": changed, "by": key,
            "manual": {"url": manual["url"], "data": manual["data"], "kind": manual["kind"]}, "path": str(path),
            "parameters": params, "notes": notes, "specs": specs["url"] if specs else "", "ranges_in": ranges_in}
