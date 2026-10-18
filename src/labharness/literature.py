"""Literature search and reading, for planning experiments and reviewing what is known.

arXiv (preprints, with full text for papers that have an HTML version) and OpenAlex (journals, with citation
counts) are queried directly over HTTPS; no account is needed. A query leaves the computer, so tools say so and
the user can switch literature search off. An OpenAlex API key is optional (it raises the free daily allowance)."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._version import __version__

ARXIV_API = "https://export.arxiv.org/api/query"
OPENALEX = "https://api.openalex.org/works"
ARXIV_HTML = ("https://arxiv.org/html/%s", "https://ar5iv.labs.arxiv.org/html/%s")
USER_AGENT = "Kelvin/%s (lab assistant for experimental physics)" % __version__
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_ID = re.compile(r"^(?:arxiv:)?\s*((?:\d{4}\.\d{4,5})|(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}))(v\d+)?$", re.I)
DOI = re.compile(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)?\s*(10\.\d{4,9}/\S+)$", re.I)
Fetch = Callable[[str], bytes]


def http_get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@dataclass
class Paper:
    id: str                         # arXiv:2604.12866 or doi:10.1021/acs.nanolett.2c02863
    title: str
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    abstract: str = ""
    url: str = ""
    doi: str = ""
    arxiv: str = ""
    cited_by: Optional[int] = None

    def cite(self) -> str:
        """Short form for the text: Shubnic et al. 2026."""
        names = [a.split()[-1] if a.split() else a for a in self.authors]
        who = names[0] + (" et al." if len(names) > 2 else " and " + names[1] if len(names) == 2 else "") if names else "?"
        return "%s %s" % (who, self.year or "n.d.")

    def line(self, n: int, abstract_chars: int = 320) -> str:
        bits = ["[%d] %s (%s). %s" % (n, self.cite(), self.venue or "preprint", self.title)]
        meta = ["id %s" % self.id]
        if self.cited_by is not None:
            meta.append("cited %d" % self.cited_by)
        if self.url:
            meta.append(self.url)
        bits.append("    " + "; ".join(meta))
        if self.abstract:
            a = self.abstract if len(self.abstract) <= abstract_chars else self.abstract[:abstract_chars].rsplit(" ", 1)[0] + " …"
            bits.append("    " + a)
        return "\n".join(bits)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", t.lower())


_JUNK = re.compile(r"^(?:High Resolution Image\s*)?(?:Download\s*)?(?:MS PowerPoint Slide\s*)?(?:Abstract[:.]?\s+)?", re.I)


def abstract_from_index(index: Optional[Dict[str, List[int]]]) -> str:
    """OpenAlex stores abstracts as {word: [positions]}."""
    if not index:
        return ""
    words: Dict[int, str] = {}
    for word, places in index.items():
        for p in places:
            words[p] = word
    return " ".join(words[i] for i in sorted(words))


def parse_arxiv(xml_bytes: bytes) -> List[Paper]:
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall(ATOM + "entry"):
        raw_id = (e.findtext(ATOM + "id") or "").rsplit("/abs/", 1)[-1]
        if not raw_id or e.findtext(ATOM + "title") is None:
            continue
        base = re.sub(r"v\d+$", "", raw_id)
        published = e.findtext(ATOM + "published") or ""
        doi = e.findtext("{http://arxiv.org/schemas/atom}doi") or ""
        journal = e.findtext("{http://arxiv.org/schemas/atom}journal_ref") or ""
        out.append(Paper(id="arXiv:" + base, title=_clean(e.findtext(ATOM + "title")),
                         authors=[_clean(a.findtext(ATOM + "name") or "") for a in e.findall(ATOM + "author")],
                         year=int(published[:4]) if published[:4].isdigit() else None,
                         venue=_clean(journal) or "arXiv", abstract=_clean(e.findtext(ATOM + "summary") or ""),
                         url="https://arxiv.org/abs/" + base, doi=doi.lower(), arxiv=base))
    return out


def parse_openalex(data: Dict[str, Any]) -> List[Paper]:
    out = []
    for w in data.get("results") or []:
        if not w.get("title"):
            continue
        doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
        arxiv = ""
        m = re.match(r"10\.48550/arxiv\.(.+)", doi)
        if m:
            arxiv, doi = m.group(1), ""
        loc = w.get("primary_location") or {}
        venue = ((loc.get("source") or {}).get("display_name") or "").replace("arXiv (Cornell University)", "arXiv")
        halves = venue.split("/")
        if len(halves) == 2 and _norm_title(halves[0]) == _norm_title(halves[1]):
            venue = halves[0]   # OpenAlex sometimes doubles a journal name
        pid = "arXiv:" + arxiv if arxiv else "doi:" + doi if doi else "openalex:" + str(w.get("id", "")).rsplit("/", 1)[-1]
        out.append(Paper(id=pid, title=_clean(w["title"]),
                         authors=[_clean((a.get("author") or {}).get("display_name") or "") for a in w.get("authorships") or []],
                         year=w.get("publication_year"), venue=venue,
                         abstract=_JUNK.sub("", _clean(abstract_from_index(w.get("abstract_inverted_index")))),
                         url=("https://arxiv.org/abs/" + arxiv) if arxiv else ("https://doi.org/" + doi if doi else
                                                                               loc.get("landing_page_url") or ""),
                         doi=doi, arxiv=arxiv, cited_by=w.get("cited_by_count")))
    return out


def merge(*lists: List[Paper]) -> List[Paper]:
    """Interleave sources, dropping a paper already listed (same arXiv id, DOI or title)."""
    seen: Dict[str, Paper] = {}
    out: List[Paper] = []
    for i in range(max((len(x) for x in lists), default=0)):
        for papers in lists:
            if i >= len(papers):
                continue
            p = papers[i]
            keys = [k for k in ("a:" + p.arxiv if p.arxiv else "", "d:" + p.doi if p.doi else "", "t:" + _norm_title(p.title)) if k]
            twin = next((seen[k] for k in keys if k in seen), None)
            if twin is not None:   # keep the richer record: journal venue, DOI and citations from OpenAlex
                twin.doi = twin.doi or p.doi
                twin.cited_by = twin.cited_by if twin.cited_by is not None else p.cited_by
                if twin.venue in ("arXiv", "") and p.venue not in ("arXiv", ""):
                    twin.venue = p.venue
                twin.abstract = twin.abstract or p.abstract
                for k in keys:
                    seen[k] = twin
                continue
            for k in keys:
                seen[k] = p
            out.append(p)
    return out


class _Text(HTMLParser):
    """arXiv HTML to plain text: headings marked with ##, equations as $LaTeX$, page furniture dropped."""
    SKIP = {"script", "style", "nav", "header", "footer", "button", "svg", "form", "dialog"}
    SKIP_CLASSES = {"ltx_page_footer", "ltx_page_header", "ltx_authors", "ltx_dates", "ltx_role_affiliation"}
    BLOCK = {"p", "div", "li", "br", "tr", "figcaption", "blockquote", "table", "section", "dd", "dt"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip = 0
        self.skip_tags: List[str] = []
        self.math = 0
        self.heading = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a = dict(attrs)
        if self.math:
            self.math += tag == "math"
            return
        cls = a.get("class") or ""
        if tag in self.SKIP or set(cls.split()) & self.SKIP_CLASSES:
            self.skip += 1
            self.skip_tags.append(tag)
            return
        if self.skip:
            if tag == self.skip_tags[-1]:
                self.skip += 1
                self.skip_tags.append(tag)
            return
        if tag == "math":
            self.math = 1
            tex = (a.get("alttext") or "").strip()
            self.parts.append(" $%s$ " % tex if tex else " ")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n\n## ")
            self.heading = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.math:
            if tag == "math":
                self.math -= 1
            return
        if self.skip:
            if tag == self.skip_tags[-1]:
                self.skip -= 1
                self.skip_tags.pop()
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")
            self.heading = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip and not self.math:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[^\S\n]+", " ", raw)   # every kind of space, but keep line breaks
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.split("\n## Instructions for reporting errors")[0].strip()


def html_to_text(html: str) -> str:
    start = html.find("<article")   # the paper itself, without the site around it
    if start >= 0:
        end = html.rfind("</article>")
        html = html[start:end + 10 if end > start else len(html)]
    p = _Text()
    p.feed(html)
    p.close()
    return p.text()


class Literature:
    """Search and read papers. Results are cached; arXiv gets at most one request every few seconds."""

    def __init__(self, fetch: Fetch = http_get, openalex_key: str = "", arxiv_gap: float = 3.0):
        self.fetch = fetch
        self.openalex_key = openalex_key
        self.arxiv_gap = arxiv_gap
        self.enabled = True
        self._known: Dict[str, Paper] = {}
        self._cache: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.Lock()
        self._last_arxiv = 0.0

    def _cached(self, key: str, make: Callable[[], Any]) -> Any:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        value = make()
        with self._lock:
            self._cache[key] = value
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
        return value

    def _arxiv(self, params: Dict[str, Any]) -> bytes:
        with self._lock:
            wait = self._last_arxiv + self.arxiv_gap - time.monotonic()
            self._last_arxiv = max(time.monotonic(), self._last_arxiv + self.arxiv_gap)
        if wait > 0:
            time.sleep(wait)
        return self.fetch(ARXIV_API + "?" + urllib.parse.urlencode(params))

    def _openalex(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if self.openalex_key:
            params = dict(params, api_key=self.openalex_key)
        return json.loads(self.fetch(OPENALEX + path + ("?" + urllib.parse.urlencode(params) if params else "")))

    def search(self, query: str, limit: int = 8, since: Optional[int] = None) -> Tuple[List[Paper], List[str]]:
        """Papers from both sources, merged; and what went wrong with either."""
        query = _clean(query)
        if not query:
            raise ValueError("say what to search for")
        limit = max(1, min(20, int(limit)))
        words = [w for w in re.findall(r"[\w.+-]+", query) if len(w) > 1][:10]
        problems: List[str] = []

        def arxiv() -> List[Paper]:
            terms = " AND ".join("all:%s" % w for w in words) or "all:" + query
            return parse_arxiv(self._arxiv({"search_query": terms, "start": 0, "max_results": limit,
                                            "sortBy": "relevance"}))

        def openalex() -> List[Paper]:
            params: Dict[str, Any] = {"search": query, "per-page": limit, "select": ",".join(
                ["id", "doi", "title", "authorships", "publication_year", "primary_location", "abstract_inverted_index",
                 "cited_by_count"])}
            if since:
                params["filter"] = "from_publication_date:%d-01-01" % int(since)
            return parse_openalex(self._openalex("", params))

        found: List[List[Paper]] = []
        for name, run in (("arXiv", arxiv), ("OpenAlex", openalex)):
            try:
                papers = self._cached("%s|%s|%s|%s" % (name, query, limit, since), run)
            except Exception as e:
                problems.append("%s did not answer (%s)" % (name, str(e).splitlines()[0][:120] if str(e) else type(e).__name__))
                continue
            if since and name == "arXiv":
                papers = [p for p in papers if not p.year or p.year >= int(since)]
            found.append(papers)
        merged = merge(*found)[:limit]
        for p in merged:
            self._known[p.id.lower()] = p
        return merged, problems

    def lookup(self, identifier: str) -> Paper:
        """A paper's record by arXiv id or DOI (from a search, or fetched)."""
        ident = identifier.strip()
        known = self._known.get(ident.lower())
        if known:
            return known
        m = ARXIV_ID.match(ident)
        if m:
            base = m.group(1)
            papers = self._cached("arxiv-id|" + base, lambda: parse_arxiv(self._arxiv({"id_list": base, "max_results": 1})))
            if not papers:
                raise KeyError("arXiv has no paper %s" % base)
            self._known["arxiv:" + base.lower()] = papers[0]
            return papers[0]
        m = DOI.match(ident)
        if m:
            doi = m.group(1).lower()
            data = self._cached("doi|" + doi, lambda: self._openalex("/doi:" + urllib.parse.quote(doi, safe="/:.()-_"), {}))
            papers = parse_openalex({"results": [data]})
            if not papers:
                raise KeyError("no paper with DOI %s" % doi)
            self._known["doi:" + doi] = papers[0]
            return papers[0]
        raise KeyError("give an arXiv id (arXiv:2401.01234) or a DOI (doi:10.1103/...), as search_papers lists them")

    def full_text(self, paper: Paper) -> str:
        """The paper's text from arXiv's HTML version; empty when there is none (or it is not on arXiv)."""
        if not paper.arxiv:
            return ""

        def get() -> str:
            for pattern in ARXIV_HTML:
                try:
                    html = self.fetch(pattern % paper.arxiv).decode("utf-8", "replace")
                except Exception:
                    continue
                text = html_to_text(html)
                if len(text) > 2000:
                    return text
            return ""
        return self._cached("html|" + paper.arxiv, get)

    def read(self, identifier: str, start: int = 0, max_chars: int = 12000) -> str:
        paper = self.lookup(identifier)
        head = [paper.title, "%s; %s%s" % (", ".join(paper.authors[:12]) + (" et al." if len(paper.authors) > 12 else ""),
                                          paper.venue or "preprint", " %s" % paper.year if paper.year else ""),
                paper.url]
        body = self.full_text(paper)
        max_chars = max(1000, min(40000, int(max_chars)))
        start = max(0, int(start))
        if not body:
            note = "(no full text available here: abstract only%s)" % (
                "; it is not on arXiv" if not paper.arxiv else "; this arXiv paper has no HTML version")
            return "\n".join(head + ["", "Abstract: " + (paper.abstract or "(none listed)"), "", note])
        if start == 0:
            sections = [line[3:] for line in body.splitlines() if line.startswith("## ")]
            head.append("Sections: " + " | ".join(s.strip()[:60] for s in sections[:30]))
        chunk = body[start:start + max_chars]
        end = start + len(chunk)
        tail = ("\n\n(continues: read_paper(paper=%r, start=%d); %d characters in all)" % (paper.id, end, len(body))
                if end < len(body) else "\n\n(end of paper)")
        return "\n".join(head) + "\n\n" + chunk + tail
