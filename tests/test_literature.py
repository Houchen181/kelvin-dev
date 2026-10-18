"""Literature search and reading (arXiv, OpenAlex), the project reading list, and references in reports."""
import json
import urllib.parse

import pytest

from labharness import build_virtual_lab, demo_envelope
from labharness.agent.approval import WebApprover
from labharness.agent.runtime import LabRuntime
from labharness.literature import Literature, Paper, html_to_text, merge, parse_arxiv, parse_openalex

ARXIV = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2604.12866v2</id>
    <title>Acoustically-driven magnons
      in CrSBr bilayers</title>
    <summary>We study the coupling between spin excitations and acoustic waves.</summary>
    <published>2026-04-14T10:00:00Z</published>
    <author><name>A. Shubnic</name></author><author><name>I. Chestnov</name></author><author><name>I. Shelykh</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2201.00001v1</id>
    <title>Magnon straintronics in CrSBr</title>
    <summary>First-principles study.</summary>
    <published>2022-01-01T00:00:00Z</published>
    <arxiv:doi>10.1021/ACS.NANOLETT.2C02863</arxiv:doi>
    <arxiv:journal_ref>Nano Lett. 22, 8771 (2022)</arxiv:journal_ref>
    <author><name>Dorye L. Esteras</name></author><author><name>J. J. Baldovi</name></author>
  </entry>
</feed>"""

OPENALEX = {"results": [
    {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1021/acs.nanolett.2c02863",
     "title": "Magnon Straintronics in the 2D van der Waals Ferromagnet CrSBr", "publication_year": 2022,
     "authorships": [{"author": {"display_name": "Dorye L. Esteras"}}, {"author": {"display_name": "J. J. Baldovi"}}],
     "primary_location": {"source": {"display_name": "Nano Letters"}, "landing_page_url": "https://doi.org/10.1021/x"},
     "abstract_inverted_index": {"High": [0], "Resolution": [1], "Image": [2], "Download": [3], "MS": [4],
                                 "PowerPoint": [5], "Slide": [6], "Strain": [7], "tunes": [8], "magnons.": [9]},
     "cited_by_count": 71},
    {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.1103/physrevb.109.104401",
     "title": "Transient magnetoelastic coupling in CrSBr", "publication_year": 2024,
     "authorships": [{"author": {"display_name": "Youn Jue Bae"}}],
     "primary_location": {"source": {"display_name": "Physical review. B/Physical Review B"}},
     "abstract_inverted_index": {"Abstract": [0], "Coherent": [1], "magnons": [2], "and": [3], "phonons.": [4]},
     "cited_by_count": 26},
    {"id": "https://openalex.org/W3", "doi": "https://doi.org/10.48550/arxiv.2604.12866",
     "title": "Acoustically-driven magnons in CrSBr bilayers", "publication_year": 2026,
     "authorships": [{"author": {"display_name": "A. Shubnic"}}],
     "primary_location": {"source": {"display_name": "arXiv (Cornell University)"}}, "cited_by_count": 0},
]}

HTML = """<html><head><title>x</title><script>var a = 1;</script></head><body>
<header class="ltx_page_header">Report an issue</header><form>Title: Description:</form>
<article class="ltx_document ltx_authors_1line">
<h1 class="ltx_title ltx_title_document">Acoustically-driven magnons in CrSBr bilayers</h1>
<div class="ltx_authors"><span class="ltx_creator">Anton Shubnic</span><span class="ltx_role_affiliation">ITMO</span></div>
<div class="ltx_abstract"><h6>Abstract</h6><p>We study the coupling.</p></div>
<section><h2 class="ltx_title"><span class="ltx_tag">I </span>Model</h2>
<p>The gap is <math alttext="\\Delta\\approx 2\\,\\mathrm{GHz}"><semantics><mi>Δ</mi><annotation>junk</annotation></semantics></math> at zero field.""" + \
    " Long text." * 300 + """</p></section>
<footer class="ltx_page_footer">Instructions for reporting errors</footer>
</article></body></html>"""


class Web:
    """Answers like the services do; can be told to fail."""

    def __init__(self, broken=()):
        self.calls = []
        self.broken = set(broken)

    def __call__(self, url):
        self.calls.append(url)
        host = urllib.parse.urlparse(url).netloc
        if any(b in host for b in self.broken):
            raise OSError("timed out")
        if "export.arxiv.org" in host:
            return ARXIV
        if "api.openalex.org" in host:
            if "/works/doi:" in url:
                return json.dumps(OPENALEX["results"][1]).encode()
            return json.dumps(OPENALEX).encode()
        if host in ("arxiv.org", "ar5iv.labs.arxiv.org"):
            if "2604.12866" in url:
                return HTML.encode()
            raise OSError("404")
        raise OSError("unexpected " + url)


def test_parsing_both_sources():
    a = parse_arxiv(ARXIV)
    assert a[0].id == "arXiv:2604.12866" and a[0].title == "Acoustically-driven magnons in CrSBr bilayers"
    assert a[0].year == 2026 and a[0].cite() == "Shubnic et al. 2026" and a[0].url == "https://arxiv.org/abs/2604.12866"
    assert a[1].doi == "10.1021/acs.nanolett.2c02863" and a[1].venue == "Nano Lett. 22, 8771 (2022)"
    assert a[1].cite() == "Esteras and Baldovi 2022"
    o = parse_openalex(OPENALEX)
    assert o[0].id == "doi:10.1021/acs.nanolett.2c02863" and o[0].abstract == "Strain tunes magnons." and o[0].cited_by == 71
    assert o[1].venue == "Physical review. B" and o[1].abstract == "Coherent magnons and phonons."
    assert o[2].id == "arXiv:2604.12866" and o[2].arxiv == "2604.12866" and o[2].venue == "arXiv"
    merged = merge(a, o)   # each paper once, whichever source listed it first
    assert [p.id for p in merged] == ["arXiv:2604.12866", "doi:10.1021/acs.nanolett.2c02863",
                                      "doi:10.1103/physrevb.109.104401"]
    assert merged[0].cited_by == 0 and merged[1].venue == "Nano Letters"   # records fill each other in


def test_full_text_from_arxiv_html():
    text = html_to_text(HTML)
    assert text.startswith("## Acoustically-driven magnons in CrSBr bilayers")
    assert "Report an issue" not in text and "Title: Description" not in text and "var a" not in text
    assert "ITMO" not in text and "Anton Shubnic" not in text      # authors come from the record instead
    assert "## I Model" in text and "$\\Delta\\approx 2\\,\\mathrm{GHz}$ at zero field" in text and "junk" not in text
    assert "reporting errors" not in text


def test_search_reads_both_and_survives_one_failing():
    web = Web()
    lit = Literature(fetch=web, openalex_key="KEY", arxiv_gap=0)
    papers, problems = lit.search("CrSBr magnon (SAW)!", limit=5, since=2023)
    assert problems == []
    q = urllib.parse.parse_qs(urllib.parse.urlparse(web.calls[0]).query)
    assert q["search_query"] == ["all:CrSBr AND all:magnon AND all:SAW"] and q["max_results"] == ["5"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(web.calls[1]).query)
    assert q["api_key"] == ["KEY"] and q["filter"] == ["from_publication_date:2023-01-01"]
    assert "arXiv:2201.00001" not in [p.id for p in papers]       # older than asked (arXiv cannot filter by year)
    lit.search("CrSBr magnon (SAW)!", limit=5, since=2023)
    assert len(web.calls) == 2                                     # the same search is cached
    papers, problems = Literature(fetch=Web(broken={"arxiv"}), arxiv_gap=0).search("CrSBr", 3)
    assert problems == ["arXiv did not answer (timed out)"] and papers
    with pytest.raises(ValueError):
        lit.search("   ")


def test_reading_in_parts_and_abstract_only():
    lit = Literature(fetch=Web(), arxiv_gap=0)
    lit.search("CrSBr", 5)
    first = lit.read("arXiv:2604.12866", max_chars=1500)
    assert first.splitlines()[0] == "Acoustically-driven magnons in CrSBr bilayers"
    assert "Sections: Acoustically-driven magnons in CrSBr bilayers | Abstract | I Model" in first
    assert "(continues: read_paper(paper='arXiv:2604.12866', start=1500)" in first
    rest = lit.read("arXiv:2604.12866", start=1500, max_chars=40000)
    assert "Sections:" not in rest and rest.endswith("(end of paper)")
    doi = lit.read("doi:10.1103/physrevb.109.104401")
    assert "Abstract: Coherent magnons and phonons." in doi and "abstract only; it is not on arXiv" in doi
    fresh = Literature(fetch=Web(), arxiv_gap=0)          # not from a search: looked up by id
    assert fresh.lookup("10.1103/PhysRevB.109.104401").title == "Transient magnetoelastic coupling in CrSBr"
    assert fresh.lookup("https://doi.org/10.1103/physrevb.109.104401").cited_by == 26
    with pytest.raises(KeyError):
        fresh.lookup("the CrSBr paper")


@pytest.fixture
def runtime(tmp_path):
    lab = build_virtual_lab(seed=1)
    rt = LabRuntime(lab, WebApprover(timeout=5), tmp_path / "data", demo_envelope(lab))
    rt.literature = Literature(fetch=Web(), arxiv_gap=0)
    return rt


def test_kelvin_searches_and_reads_over_mcp(runtime):
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client
    from labharness.agent.server import build_server
    from test_agent import call

    async def tools():
        async with Client(build_server(runtime)) as client:
            return {t.name: t for t in (await client.list_tools()).tools}
    listed = anyio.run(tools)
    assert listed["search_papers"].annotations.read_only_hint and listed["search_papers"].annotations.open_world_hint
    found, read = call(runtime, ("search_papers", {"query": "CrSBr magnons", "limit": 3}),
                       ("read_paper", {"paper": "arXiv:2604.12866", "max_chars": 1000}))
    assert found.startswith("[1] Shubnic et al. 2026 (arXiv). Acoustically-driven magnons in CrSBr bilayers")
    assert "id doi:10.1021/acs.nanolett.2c02863; cited 71" in found and "cite as [Author Year](url)" in found
    assert "continues: read_paper" in read
    runtime.literature.enabled = False
    assert call(runtime, ("search_papers", {"query": "x"}))[0] == "literature search is switched off in Settings"


def test_reading_list_in_projects_and_reports(runtime, tmp_path):
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    from labharness.web.settings import Settings
    from labharness.web.store import ChatStore
    from test_reports import pdf_strings
    from test_web import Scripted, client_for, stream_turn
    say = lambda t: [{"type": "text", "text": t}]
    call = lambda name, args, i: [{"type": "tool_call", "id": i, "name": name, "input": args}]
    bot = Scripted([call("search_papers", {"query": "CrSBr magnons"}, "s1"),
                    call("save_paper", {"paper": "doi:10.1021/acs.nanolett.2c02863", "why": "strain tuning"}, "s2"),
                    call("save_paper", {"paper": "doi:10.1021/acs.nanolett.2c02863"}, "s3"), say("Kept it."),
                    say("Plan ready."), say("Hi.")])
    store = ChatStore(tmp_path / "chats")
    settings = Settings(tmp_path / "settings.json")
    client, headers = client_for((runtime, settings, store), {"scripted": bot})
    with client:
        project = client.post("/api/projects", json={"name": "SAW on CrSBr"}, headers=headers).json()
        chat = client.post("/api/chats", json={"model": "scripted:m1", "project": project["id"]}, headers=headers).json()
        events = stream_turn(client, headers, chat["id"], "what is known about SAW-driven magnons? keep the key paper",
                             "scripted:m1")
        ends = [e for e in events if e["type"] == "tool_end"]
        assert ends[1]["content"].startswith("added to the reading list: Dorye L. Esteras and J. J. Baldovi (2022).")
        assert ends[2]["content"].startswith("already on the reading list")
        papers = client.get("/api/projects/" + project["id"], headers=headers).json()["papers"]
        assert len(papers) == 1 and papers[0]["why"] == "strain tuning" and papers[0]["year"] == 2022
        assert "save_paper" in bot.tool_names[0] and "search_papers" in bot.tool_names[0]

        stream_turn(client, headers, chat["id"], "plan it", "scripted:m1")
        assert "## Reading list" in bot.systems[-1] and "[id doi:10.1021/acs.nanolett.2c02863] — strain tuning" in bot.systems[-1]
        loose = client.post("/api/chats", json={"model": "scripted:m1"}, headers=headers).json()
        stream_turn(client, headers, loose["id"], "hello", "scripted:m1")
        assert "save_paper" not in bot.tool_names[-1] and "Reading list" not in bot.systems[-1]

        runtime.run("plan: scan\nsweep: {stage.position: {from: 0, to: 2, step: 1}}\nmeasure: [lockin.X]\n")
        r = client.post("/api/reports", json={"scope": "project", "chat_id": chat["id"]}, headers=headers)
        assert r.status_code == 400                                      # no runs in the project's chats yet
        files = runtime.report(runtime.pick_runs(last=1), references=["Esteras and Baldovi (2022). Magnon. Nano Letters."])
        drawn = pdf_strings(runtime.report_path(files[0]["name"]))
        assert drawn[drawn.index("References") + 1:drawn.index("References") + 3] == \
            ["[1]", "Esteras and Baldovi (2022). Magnon. Nano Letters."]

        assert client.delete("/api/projects/%s/papers/%s" % (project["id"], papers[0]["id"]), headers=headers).json() == {"ok": True}
        assert client.delete("/api/projects/%s/papers/%s" % (project["id"], papers[0]["id"]), headers=headers).status_code == 404

        r = client.post("/api/settings", json={"literature": "off", "openalex_api_key": "secret-key-1"}, headers=headers)
        assert r.json()["settings"]["literature"] is False and "secret-key-1" not in r.text
        assert runtime.literature.enabled is False and runtime.literature.openalex_key == "secret-key-1"
