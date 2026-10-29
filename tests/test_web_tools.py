"""Kelvin on the web: web_search through a model service's own search, read_web_page, save_manual, the manual search
of draft_card trying what the web search found; and screenshots that need no image library."""
import sys
import types
import zlib

import pytest

pytest.importorskip("mcp")
pytest.importorskip("pypdf")
import anyio  # noqa: E402
from labharness import autocard, claudecode, screen  # noqa: E402
from labharness.agent.server import build_server  # noqa: E402
from labharness.web import websearch  # noqa: E402
from labharness.web.agent import WEB_TOOLS, ChatAgent  # noqa: E402
from labharness.web.providers.base import ModelInfo, Provider  # noqa: E402
from labharness.web.store import ChatStore  # noqa: E402
from test_autocard import MANUAL  # noqa: E402
from test_autosetup import runtime_for, tiny_pdf  # noqa: E402
from test_web import DONE, Scripted  # noqa: E402


class Searcher(Provider):
    """A model service with a web search of its own."""
    label, can_search = "Searcher", True

    def __init__(self, name, results=(), error=None):
        self.name, self.results, self.error, self.asked = name, list(results), error, []

    def available(self):
        return True

    async def models(self):
        return [ModelInfo("s1", "S1", self.name)]

    async def search(self, model, query, limit=8):
        self.asked.append((model, query, limit))
        if self.error:
            raise RuntimeError(self.error)
        return self.results[:limit]


HIT = [{"title": "TPG 361/362 Communication protocol", "url": "https://x.example/bg5510.pdf", "snippet": "ENQ"}]


def test_results_from_a_models_answer():
    text = ("- Pfeiffer TPG 361/362 Communication protocol | https://x.example/bg5510.pdf | the ACK/ENQ protocol\n"
            "2. [TPG 362 product page](https://www.pfeiffer.example/tpg-362) - the product page\n"
            "no address on this line\n- the same again | https://x.example/bg5510.pdf | twice")
    found = websearch.results_in(text)
    assert [r["url"] for r in found] == ["https://x.example/bg5510.pdf", "https://www.pfeiffer.example/tpg-362"]
    assert found[0]["title"] == "Pfeiffer TPG 361/362 Communication protocol" and found[0]["snippet"] == \
        "the ACK/ENQ protocol"
    assert found[1]["title"] == "TPG 362 product page" and found[1]["snippet"] == "the product page"


def test_the_chats_own_service_searches_first_then_the_others():
    broken = Searcher("anthropic", error="web search is not enabled for this organization")
    good = Searcher("openai", HIT)
    found, by, problems = anyio.run(websearch.search, {"anthropic": broken, "openai": good}, "openai:gpt-x", "q", 5)
    assert found == HIT and good.asked == [("gpt-x", "q", 5)] and broken.asked == []
    found, by, problems = anyio.run(websearch.search, {"anthropic": broken, "openai": good}, "local:llama", "q", 5)
    assert found == HIT and broken.asked == [("s1", "q", 5)] and "not enabled" in problems[0]
    assert "TPG 361/362 Communication protocol — https://x.example/bg5510.pdf" in websearch.describe(found, by, [])
    assert "no web results" in websearch.describe([], "", []) and "Settings" in websearch.describe([], "", [])


def test_each_services_own_search(tmp_path, monkeypatch):
    from labharness.web.providers.anthropic_provider import AnthropicProvider
    from labharness.web.providers.claudecode_provider import ClaudeCodeProvider
    from labharness.web.providers.gemini_provider import GeminiProvider
    from labharness.web.providers.openai_provider import OpenAIProvider
    ns, calls = types.SimpleNamespace, []

    async def returns(value, **kw):
        calls.append(kw)
        return value

    claude = AnthropicProvider("sk-test")   # Claude's web search: a server tool, results in its own blocks
    blocks = [ns(type="server_tool_use"),
              ns(type="web_search_tool_result", content=[ns(type="web_search_result", title="BG 5510",
                                                             url="https://x.example/bg5510.pdf")]),
              ns(type="text", text="- The protocol | https://y.example/protocol.pdf | ACK and ENQ")]
    claude.client = ns(messages=ns(create=lambda **kw: returns(ns(content=blocks), **kw)))
    found = anyio.run(claude.search, "claude-x", "TPG 362 protocol", 5)
    assert [r["url"] for r in found] == ["https://y.example/protocol.pdf", "https://x.example/bg5510.pdf"]
    assert calls[-1]["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    failed = [ns(type="web_search_tool_result", content=ns(type="web_search_tool_result_error",
                                                            error_code="max_uses_exceeded"))]
    claude.client = ns(messages=ns(create=lambda **kw: returns(ns(content=failed), **kw)))
    with pytest.raises(RuntimeError, match="max_uses_exceeded"):
        anyio.run(claude.search, "claude-x", "q", 5)

    gpt = OpenAIProvider("openai", "GPT", "sk-test")   # OpenAI: the Responses API's web_search, its citations
    cited = ns(type="message", content=[ns(annotations=[ns(type="url_citation", title="TPG 362",
                                                           url="https://z.example/tpg362")])])
    gpt.client = ns(responses=ns(create=lambda **kw: returns(ns(output=[ns(type="web_search_call"), cited],
                                                                 output_text=""), **kw)))
    assert anyio.run(gpt.search, "gpt-x", "q", 5)[0]["url"] == "https://z.example/tpg362"
    assert calls[-1]["tools"] == [{"type": "web_search"}]
    assert not OpenAIProvider("local", "Local", None, base_url="http://127.0.0.1:1234/v1").can_search

    gemini = GeminiProvider("key")   # Gemini: Google Search, the pages its answer is grounded on
    chunk = ns(web=ns(uri="https://grounding.example/r1", title="pfeiffer-vacuum.com", domain="pfeiffer-vacuum.com"))
    answer = ns(candidates=[ns(grounding_metadata=ns(grounding_chunks=[chunk]))], text="")
    gemini.client = ns(aio=ns(models=ns(generate_content=lambda **kw: returns(answer, **kw))))
    assert anyio.run(gemini.search, "gemini-x", "q", 5) == [
        {"title": "pfeiffer-vacuum.com", "url": "https://grounding.example/r1", "snippet": ""}]
    assert calls[-1]["config"].tools[0].google_search is not None

    runs = []   # Claude Code: one claude -p run with only WebSearch allowed

    async def fake_stream(cmd, args, stdin_text, cwd, should_stop=None, timeout=600):
        runs.append(args)
        yield {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch"}]}}
        yield {"type": "result", "subtype": "success", "result": "- Protocol | https://w.example/bg5510.pdf | ENQ"}
    monkeypatch.setattr(claudecode, "stream", fake_stream)
    found = anyio.run(ClaudeCodeProvider(["claude"], tmp_path).search, "sonnet", "q", 5)
    assert found[0]["url"] == "https://w.example/bg5510.pdf"
    assert runs[0][runs[0].index("--tools") + 1] == "WebSearch" and "--allowedTools" in runs[0]


def test_a_chat_searches_the_web_and_a_confidential_project_does_not(tmp_path):
    runtime = runtime_for(tmp_path)
    store = ChatStore(tmp_path / "chats")
    search = [{"type": "tool_call", "id": "c1", "name": "web_search", "input": {"query": "TPG 362 protocol pdf"}}]
    model = Scripted([search, DONE])

    async def main():
        agent = ChatAgent(runtime, store, build_server(runtime), {"scripted": model,
                                                                  "anthropic": Searcher("anthropic", HIT)})
        await agent.start()
        try:
            chat = store.create("scripted:m1")
            await agent.run_turn(chat["id"], "find the TPG 362 manual", "scripted:m1",
                                 agent.claim(chat["id"], "scripted:m1"), lambda e: anyio.sleep(0))
            return store.load(chat["id"])
        finally:
            await agent.close()
    chat = anyio.run(main)
    result = [r for m in chat["messages"] if m["role"] == "tool" for r in m["results"]][0]
    assert "web_search" in model.tool_names[0] and not result["is_error"]
    assert "https://x.example/bg5510.pdf" in result["content"] and "save_manual" in result["content"]
    assert {"web_search", "read_web_page", "save_manual"} <= WEB_TOOLS   # off where a project keeps off the internet


def test_read_a_page_then_keep_the_manual(tmp_path):
    from labharness.agent.approval import WebApprover
    from labharness.agent.runtime import LabRuntime
    from simlab import build_virtual_lab
    rt = LabRuntime(build_virtual_lab(seed=1), WebApprover(timeout=5), tmp_path / "data")   # the test lab's smu
    rt.home = tmp_path
    manual = tiny_pdf(MANUAL)
    pages = {"https://www.acme.example/support/w9000":
             b'<html><body><h1>Widget 9000 support</h1><p>Downloads for the Widget 9000.</p>'
             b'<a href="/docs/brochure.html">Brochure</a><a href="/docs/W9000-prog.pdf">Programming manual</a>'
             b'</body></html>',
             "https://www.acme.example/docs/W9000-prog.pdf": manual}
    rt.fetch_url = lambda url: pages[url]
    page = rt.read_web_page("https://www.acme.example/support/w9000")
    links = page.split("Links (documents first):")[1]
    assert "Downloads for the Widget 9000" in page and links.index("W9000-prog.pdf") < links.index("brochure.html")
    assert "a PDF of 3 pages" in rt.read_web_page("https://www.acme.example/docs/W9000-prog.pdf")
    assert rt.read_web_page("file:///C:/Windows/win.ini").startswith("give a web address")
    out = rt.save_manual("https://www.acme.example/docs/W9000-prog.pdf", "ACME", "WIDGET 9000",
                         address="GPIB0::5::INSTR")
    assert (tmp_path / "manuals" / "ACME-WIDGET-9000.pdf").read_bytes() == manual
    assert "command reference" in out and "draft_card GPIB0::5::INSTR with manual=" in out
    assert rt.manuals_found["GPIB0::5::INSTR"]["data"] == manual          # add_instrument keeps it with the instrument
    assert "does not name the GIZMO 1" in rt.save_manual("https://www.acme.example/docs/W9000-prog.pdf", "ACME", "GIZMO 1")
    out = rt.save_manual("https://www.acme.example/docs/W9000-prog.pdf", "ACME", "WIDGET 9000", instrument="smu")
    assert "now smu's manual" in out and (tmp_path / "instruments" / "smu" / "manual.pdf").exists()
    rt.literature.enabled = False
    assert "switched off" in rt.read_web_page("https://www.acme.example/support/w9000")


def test_the_manual_search_tries_what_the_web_search_found():
    """The model remembers an address that no longer exists; the web search finds a copy elsewhere."""
    pages = {"https://mirror.example/library/W9000_programming.pdf": tiny_pdf(MANUAL)}

    def fetch(url):
        if url not in pages:
            raise OSError("HTTP Error 404: Not Found")
        return pages[url]
    queries = []
    found = autocard.find_manual("ACME", "WIDGET 9000", lambda s, p: ("https://www.acme.example/gone.pdf", "m"), fetch,
                                 search=lambda q: queries.append(q) or list(pages))
    assert found["url"] == "https://mirror.example/library/W9000_programming.pdf"
    assert queries == ["ACME WIDGET 9000 programming manual pdf"]


def test_screenshots_need_no_image_library():
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])   # red, green / blue, white
    data = screen.png(rgb, 2, 2)
    assert screen.png_size(data) == (2, 2)
    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    assert zlib.decompress(idat) == b"\x00" + rgb[:6] + b"\x00" + rgb[6:]   # one filter byte per row
    try:
        from PIL import Image   # where Pillow happens to be installed, it reads Kelvin's PNG the same way
        import io
        assert Image.open(io.BytesIO(data)).convert("RGB").getpixel((0, 1)) == (0, 0, 255)
    except ImportError:
        pass
    if sys.platform != "darwin" and not sys.platform.startswith("win"):
        with pytest.raises(RuntimeError, match="Windows and macOS"):
            screen.screenshot_png()
