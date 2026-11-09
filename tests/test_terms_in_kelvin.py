"""The Kelvin hub's terms are agreed to in Kelvin itself, never on a page of the hub's the person would sign in to
(asked for 2026-11-09: "the term agreement should be included during the kelvin desktop installation process, not
letting user to login to hub to agree a term specifically"). Create a Kelvin account has its box; signing in when the
person's agreement is to older terms shows the terms in force as a step of the sign-in screen; the notice's Read and
agree, Settings → Account and Settings → Models open the same in a dialog. An older hub, which says no version, still
takes the agreement on its own page."""
import pytest

pytest.importorskip("starlette")

from labharness.web import hubsync  # noqa: E402
from labharness.web.app import STATIC  # noqa: E402

PAGE = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _js(name):
    """One function of app.js, by its name."""
    start = JS.index("function %s(" % name)
    return JS[start:JS.index("\n  }\n", start)]


def _part(start, end):
    i = PAGE.index(start)
    return PAGE[i:PAGE.index(end, i)]


def test_the_sign_in_screen_asks_for_the_terms_in_force_as_a_step():
    form = _part('<form id="central-terms"', "</form>")
    for part in ('id="ct-changed"', 'id="ct-consent" type="checkbox"', 'id="ct-terms"', 'id="ct-privacy"',
                 'id="ct-later"', 'id="ct-agree" class="btn primary" type="submit" disabled>Agree'):
        assert part in form, part
    assert PAGE.index('<form id="central-new"') < PAGE.index('<form id="central-terms"') < PAGE.index('id="central-link"')
    central = JS[JS.index("function showCentral(session)"):JS.index("function setupRemove(")]
    assert '"central-terms"' in central and 'terms: ["central-terms"] }[mode]' in central
    assert 'terms: "The Kelvin hub\'s terms changed" }[mode]' in central
    # after signing in: the terms first (the hub said their version), then the rest of signing in
    after = central[central.index("const after = (r) => {"):central.index("const showTerms")]
    assert after.index("if (r.needs_consent && r.terms && r.terms.consent_version) { showTerms(r); return; }") < \
        after.index("if (r.keys_sealed)")
    terms = central[central.index("const showTerms = (r) => {"):central.index('view("terms");\n    };')]
    assert 'await api("/api/hub/terms", { body: { version: t.consent_version } })' in terms
    assert "next = () => after(Object.assign({}, r, { needs_consent: false }))" in terms
    assert '$("ct-later").onclick = () => { say(""); next(); };' in terms     # Not now: the notice waits for it
    assert 'if (err.status === 409 && err.data && err.data.terms) showTerms(' in terms   # changed again: read first
    assert '$("ct-agree").disabled = !$("ct-consent").checked;' in terms


def test_the_notice_settings_and_models_agree_here_in_a_dialog():
    bar = _part('<div id="hub-terms-bar"', "</div>")
    assert '<button id="hub-terms-agree" class="link soft" type="button">Read and agree</button>' in bar
    assert "target=" not in bar and "href" not in bar                    # nothing sends the person to the hub to agree
    dialog = _part('<dialog id="terms-dialog"', "</dialog>")
    for part in ('id="terms-changed"', 'id="terms-consent" type="checkbox"', 'id="terms-read"', 'id="terms-privacy"',
                 'id="terms-msg"', 'id="terms-agree" class="btn primary" type="button" disabled>Agree'):
        assert part in dialog, part
    assert '<button id="hub-agree" class="link soft" type="button" hidden>Read and agree to the new terms</button>' in PAGE
    opened = _js("openTerms")
    assert "if (!t.consent_version) { if (t.consent_url) { window.open(t.consent_url" in opened   # an older hub
    assert '$("terms-dialog").dataset.version = t.consent_version;' in opened
    agree = _js("agreeTerms")
    assert 'await api("/api/hub/terms", { body: { version: $("terms-dialog").dataset.version } })' in agree
    assert "hubTerms(null);" in agree and 'if (e.status === 409 && e.data && e.data.terms) await openTerms(e.data.terms);' in agree
    assert '$("hub-terms-agree").addEventListener("click", () => openTerms(HUB.terms));' in JS
    assert '$("hub-agree").addEventListener("click", () => openTerms(HUB.st && HUB.st.account));' in JS
    assert 'h("button", { class: "link", type: "button", onclick: () => openTerms(hub) }, "agree to the hub\'s new terms")' \
        in _js("showHubUsage")
    show = _js("showHub")
    assert '$("hub-account").href = a ? a.account_url :' in show and "consent_url" not in show
    assert '$("hub-agree").hidden = !(a && a.needs_consent);' in show


def test_the_sync_says_to_agree_in_kelvin_and_names_the_hubs_pages_at_its_own_address():
    assert "in Kelvin" in hubsync.TERMS and "on the hub" not in hubsync.TERMS
    t = hubsync.terms_of("https://hub.test", {"consent_url": "http://elsewhere/consent", "consent_version": "2099-01-01",
                                              "terms_changed": "Kelvin now shares", "terms_url": "http://elsewhere/t"})
    assert t == {"consent_url": "https://hub.test/consent", "consent_version": "2099-01-01",
                 "terms_changed": "Kelvin now shares", "terms_url": "https://hub.test/terms",
                 "privacy_url": "https://hub.test/privacy"}
    assert hubsync.terms_of("https://hub.test", None)["consent_version"] == ""   # an older hub: its own page
