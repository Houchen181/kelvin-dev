"""Buttons in a row, as the kit draws them (app.css): a skill's Show and Download are small buttons on one line under
what it is, with room around them, in Settings → Memory and a project's settings alike. The other rows whose text
buttons (button.link, outlined since "text buttons look like buttons") stood stacked, stretched, split or cramped:
the help's topics, a project's master report, the owner's PIN and notifications, the scan editor's + Add."""
import re

import pytest

pytest.importorskip("starlette")

from labharness.web.app import STATIC  # noqa: E402

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


def _fn(src, name):
    start = src.index("function %s(" % name)
    return src[start:src.index("\n  }\n", start)]


def _rule(selector):
    """The declarations of app.css's rule for one selector."""
    found = re.search(r"(?m)^%s \{([^}]*)\}" % re.escape(selector), CSS)
    assert found, selector
    return found.group(1)


def test_a_skills_show_and_download_are_small_buttons_on_one_line():
    know = _fn(JS, "renderKnow")
    assert 'const show = h("button", { class: "btn small", type: "button" }, "Show");' in know
    assert ('const get = h("button", { class: "btn small", type: "button", title: "As a .zip for Claude or another '
            'Kelvin" }, "Download");') in know
    assert 'const acts = h("div", { class: "field-actions" }, show);' in know and "acts.append(get);" in know
    assert 'h("span", { class: "memory-meta" }, when), acts, body)' in know   # under what it is; its steps under them
    assert "link soft" not in know
    line = _rule(".field-actions")   # one line (wrapping only when the row is narrow), 8px apart, centred
    assert all(d in line for d in ("display: flex", "gap: 8px", "flex-wrap: wrap", "align-items: center"))
    assert "margin-top: 6px" in _rule(".memory-body > .field-actions")
    assert ".memory-body > .link" not in CSS   # the text links' padding (2px 0), which cramped them in their outlines
    assert 'loadKnow("global", $("global-know")' in JS and 'loadKnow(p.id, $("project-know")' in JS   # the same rows
    for list_id in ("global-know", "project-know"):
        assert '<ul id="%s" class="memory-notes"></ul>' % list_id in PAGE


def test_rows_of_outlined_text_buttons_have_room_and_stay_together():
    # the help's topics: buttons with room between them, and under their document's name
    assert "gap: 6px 8px" in _rule(".help-links") and "gap: 6px" in _rule(".help-doc")
    assert 'h("div", { class: "help-links" }, ...items.map((t) => {' in JS
    # a project's master report: Open, Write with Kelvin and Update now on one line, never stretched or stacked as
    # the grid of a form's row (.add-row) drew them
    master = PAGE[PAGE.index('<textarea id="project-summary"'):]
    master = master[master.index("<div"):master.index("</div>") + 6]
    assert master.startswith('<div class="field-actions"><button id="master-open" class="btn small"')
    assert 'id="summary-write"' in master and 'id="master-update" class="link soft"' in master
    # the owner's PIN and notifications: their buttons together at the row's end, not split by its space-between
    for first, second in (("owner-pin-set", "owner-pin-remove"), ("owner-notify-mine", "owner-notify-off")):
        row = re.search(r'<span class="field-actions"><button id="%s"[^>]*>[^<]*</button>\s*<button id="%s"[^>]*>'
                        r'[^<]*</button></span></div>' % (first, second), PAGE)
        assert row, first
    assert "justify-content: space-between" in _rule(".field-row")
    # the scan editor's + Loop and + Add, and the step row: no text-link padding cramping an outlined button
    assert ".ed-sec-head .link" not in CSS and ".step-foot .link" not in CSS
    assert 'id="ed-add-loop" class="link soft"' in PAGE
    assert "padding: 4px 10px" in _rule("button.link")
