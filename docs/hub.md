# The Kelvin hub

The hub is the Kelvin team's server for **Kelvin accounts**: one email and password that signs a person in to Kelvin on every computer ([portal.md](portal.md#kelvin-accounts-one-account-every-computer)). There is one hub for every Kelvin user in every lab, built into Kelvin (`central.DEFAULT_HUB`) and the iPhone app (`Account.defaultHub`); labs run none of their own. It is the owners' tool, not a place for users: the **owner console** (`/admin`) shows everyone's account, the computers each signs in on, what happened to it, and what their Kelvins shared. It is built for a few thousand people (about 5,000): one SQLite file.

**Where it runs:** `https://kelvin.tail269ee5.ts.net`, on a lab workstation through Tailscale Funnel, for now. That carries light traffic only (Funnel has fixed bandwidth limits), so before other labs join, the hub moves to an always-on server with its own domain ([Putting it online](#putting-it-online)), and an update of Kelvin and the iPhone app changes the address they have built in.

**What users see:** only what an account needs. That is creating one (in Kelvin, or on the hub), the email confirmation and password reset links, and a short account page: name and ORCID iD, password, what their Kelvins shared, the Kelvins they sign in to, export and delete. No user sees another's work.

**Sharing, off unless the person switches it on (since Kelvin 0.3.51; from 2026-11-08 it was on by default):** Kelvin shares each signed-in person's normal projects, unfiled chats with their whole conversations, and the records of their finished runs with the Kelvin team here, once the person switches it on in Kelvin's Settings → **Account** ([In the Kelvin app](#in-the-kelvin-app)). The Kelvin team reads it in the owner console ([The owner console](#the-owner-console)). Three switches in `hub.json` decide what the hub takes and who sees it:
- **`"sharing": true`** (the default): Kelvins share work here. With `false`, the hub keeps Kelvin accounts only: shared work's pages and API calls answer 404, the account page and owner console leave it out, and the terms and privacy notice are the accounts-only ones. **A `hub.json` written by an older hub says `"sharing": false`: set it to `true`.**
- **`"labs": false`** (the default): no labs. Shared work is seen by its person and the Kelvin team only, and labs' pages, join codes and API calls answer 404. With `true`, people make and join labs, whose members see each other's shared work ([Accounts and labs](#accounts-and-labs)).
- **`"run_files": false`** (the default): the hub keeps runs' records only, and refuses their data files (403), which stay on people's computers. With `true`, it takes them too, from Kelvins that still send them.
- **`"contact"`**: an email address, which the terms and privacy notice name for questions and requests about people's data. Unset (the default), they leave it out.

Changing `labs` or `run_files` changes the built-in terms: raise `consent_version` with it, so everyone agrees again.

**Status:**
- **Built and tested:** the server, its database, a Kelvin's client (`hub/client.py`), Kelvin's sign-in with Kelvin accounts (`central.py`), sharing (syncing in the background: `web/hubsync.py`), and the owner console's pages for shared work.
- **The terms and privacy notice:** final since 2026-11-13, for the App Store: the privacy notice covers Kelvin for iPhone too (the phone's way through the hub, and the AI companies chats go to). Set `contact` so they name whom to write to.
- **To do:** moving the hub to a server for good ([Moving the hub to a server](#moving-the-hub-to-a-server)); Kelvin's built-in address (`central.DEFAULT_HUB`) then changes with an update.

## What is shared

- **Shared by default:** normal projects, unfiled chats with their whole conversations, and the records of finished runs (each run's `meta`: plan, status, settings, results and assessment).
- **Never sent:**
  - projects marked unpublished or confidential, and projects whose **Who may see this project** limits the models or literature search;
  - incognito chats, and chats that ran under such limits;
  - runs' data files (`run.h5`, plots, reports), while `run_files` is off: the hub refuses them (403);
  - API keys, and ChatGPT or Claude Code sign-ins.

  Kelvin's client (`collect` in `hub/client.py`) leaves these out, and sends an unshared project only as its id and "not shared", never its name.

  The hub checks the marks again on what arrives:
  - it refuses chats marked incognito, unpublished, confidential or not shared, or with model limits;
  - it refuses runs marked incognito, unpublished or confidential, in the run or its `meta`;
  - it refuses chats and runs in a project that isn't shared, and deletes any copy it had of them;
  - when a project stops being shared, it deletes everything it had of it.

  It can only check the marks Kelvin sends: an unmarked chat or run is taken as shared.
- **Marking a project:** in Kelvin, a project's **Project settings → On the Kelvin hub** is *Shared* (the default), *Unpublished* or *Confidential*. A chat moved out of an unpublished, confidential or model-limited project, or left unfiled when that project is deleted, keeps the mark (or the project's model limits) and stays unshared. Changing a project's limits locks none of its chats. A run started (or queued) from an incognito chat, or from a chat that keeps an unshared project's marks or limits, says so in its `meta.json`, and is never sent. Every run also keeps the chat that started it and the rules and marks it ran under (`chat`, `rules`, `confidential`, `unpublished`), so it stays unshared when its project's marks change later. Nor is a run whose project is gone, since Kelvin can't tell whether that project was shared.
- **Who sees it:** its person, and the Kelvin team in the owner console. While `labs` is on, also the other members of the lab it was shared in, while both are members; a Kelvin connected with "No lab" shares only with its person and the Kelvin team.
- **Taking it back:** switching sharing off in Kelvin removes what that Kelvin shared, and a chat or run deleted in Kelvin goes from the hub at the next sync. A sync that only removes is taken even before the person agrees to new terms. **Delete account** removes everything.
- **Which lab** (while `labs` is on): everything a Kelvin shared is filed under the lab it shares into now. Connecting it again with another lab, or with "No lab", moves all of it at once. Kelvin's own sign-in shares into no lab.
- **Leaving a lab** takes your work out of it for good: joining again doesn't bring it back, only connecting a Kelvin into that lab again does.
- **Labs switched off:** work shared into a lab before keeps its lab, but nobody sees it through the lab; switching `labs` on again shows it as before. A Kelvin that connects meanwhile shares into no lab, whatever it asks.

## Accounts and labs

- **Registering:** name, email, an optional ORCID iD (its check digit is checked) and a password of 8 characters or more. Agreeing to the terms and the privacy notice is required, and the hub records which version and when. Beside the box nothing more: the terms and the privacy notice say what Kelvin shares and what never goes.
- **New terms:** when the version in force goes up, everyone is asked to agree again before going on, in Kelvin itself: nobody signs in to the hub's pages to agree (see **The hub's terms** below; the hub's `/consent` page stays for its own pages and for older Kelvins). Until they do, syncing takes nothing new (a sync that only removes still goes), and the hub's models and PDF drawing wait too.
  - **The version in force:** the later of `consent_version` in `hub.json` and the code's for the terms served: `CONSENT_VERSION` (2026-11-08) with sharing, `CONSENT_VERSION_ACCOUNTS` (2026-10-29) for accounts only. A `hub.json` written by an older hub pins 2026-10-29, so switching sharing on asks everyone again; the operator can still raise it past the code's for terms of their own.
  - **Dates:** versions are dates (YYYY-MM-DD) and compare as text. Only a later one asks again, so switching sharing off asks nobody.
  - **What changed:** the terms say it at their top ("Changed on 2026-11-08: Kelvin can share your work here, if you switch sharing on"; since Kelvin 0.3.51 sharing is off unless switched on, which shares less than the terms of that version said, so their version stayed). Kelvin's step and dialog and the hub's consent page show the box, with the terms and the privacy notice to read, and nothing more.
- **Passwords:** PBKDF2-SHA256 with 600,000 iterations, as OWASP suggests.
  - **Older hashes:** a hash made with fewer is redone at the person's next sign-in, unless the password changed meanwhile.
  - **Timing:** checking it takes as long as a current one, as does checking an email that has no account. Browser sessions, app tokens and one-use codes are stored only as their SHA-256. A one-use code works once, even when two requests bring it at the same moment.
- **Email:** the hub sends a confirmation link, and password reset links. Until an SMTP server is set, or when sending fails, these go to `outbox/` in the hub's folder for the operator to pass on. `"require_confirmed_email": true` then stops unconfirmed people from connecting a Kelvin or sharing.
- **Resetting a password:** the reset page gives the same answer, and as fast, whether or not the account exists; the mail goes out after the answer. A new password that won't do leaves the link working. A reset signs out every browser and disconnects every Kelvin of the account. A password change in the account page signs out the other browsers but keeps the Kelvins connected.
- **Labs** (only while `labs` is on) **have no owner.** Anyone can make a lab. Everyone in it sees its join code (like `K7Q2-9XMB-4TRA`) and can pass it on. A lab nobody is in any more is deleted.
- **Your data:** **Export everything** on the account page downloads a zip of the account, labs, connections, everything shared and the hub's record of the account's activity, which also says each time the Kelvin team opened one of the person's chats or runs. It can be made once every 10 minutes.
- **Deleting an account:** **Delete account** (with the password) deletes the account and all it shared, even if the person hasn't agreed to new terms. Its files are deleted after the database is updated, so a large account never holds up anyone else's sign-in or sync.
  - **Also removed:** the Kelvins only that account used, and mail to it waiting in `outbox/`.
  - **Timing:** it waits for the person's sync or upload in progress, so no file lands afterwards.
  - **The log:** `hub.log` never has addresses. The activity record keeps only that account number N was deleted, without addresses or details. Account numbers are never reused. Database backups keep a deleted account until they age out (30 days by default).

## Signing in to Kelvin

Kelvin's sign-in screen talks to the hub with JSON (`central.py`; routes `api_signin`, `api_register`, `api_reset` and `api_password` in `hub/server.py`):

- **`POST /api/signin`** `{email, password, installation: {id, name, platform, version}}` returns `{access_token, user: {name, email, email_confirmed, consent_version}, needs_consent, account_url}`, and while `needs_consent` the terms in force: `consent_version`, `terms_url` and `privacy_url`.
  - **The installation's shape:** an object whose `id` is the Kelvin's installation id, for example `{"id": "urn:uuid:…", "name": "Lab PC", "platform": "win32", "version": "0.3.12"}`. This differs from `/connect`, whose query sends the id as `installation=`. Without an `id` the hub answers 400.
  - **The token:** one per account and installation; signing in again replaces it. Kelvin keeps it in the person's settings, encrypted with their password, to change the password later.
  - **The installation** is recorded as a connected Kelvin, without a lab, so the account page lists the computers.
  - **Sharing:** while sharing is on, the same token shares the person's work (`POST /api/sync`), into no lab. No `/connect` step.
  - **Limits:** a wrong password is 401, and counts toward the same limits as the sign-in page (10 per email or address in 15 minutes, then 429). A disabled account is 403. So is an unconfirmed one, once `require_confirmed_email` is on.
- **`POST /api/register`** `{name, email, password, consent: true, orcid?, installation}` makes the account, mails the confirmation link, and signs in as above. When the hub wants the address confirmed first, it answers 403 with `registered: true`. Registrations count toward the same limit as the registration page.
- **`POST /api/reset`** `{email}` mails a reset link and always answers `{ok: true}`, so nobody can probe for accounts.
- **`POST /api/password`** `{old, new}`, with the token, changes the password. Kelvins stay signed in, as with a change on the account page. A reset by mail ends every token.

Kelvin, for its part:
- **Unreachable:** a network error, a 404, any 5xx (such as a tunnel's 530), or an answer that isn't JSON means the hub can't be reached now. Kelvin then checks the password against the hash it kept at the person's last sign-in on this computer. A hub that answers no (401, 403, 429) is never overruled by that hash.
- **Wrong passwords** wait a second before Kelvin answers.

## Phones through the hub

A phone signed in to a person's Kelvin account reaches every Kelvin where that account is signed in, through the hub (`hub/relay.py`, and Kelvin's side `web/relay.py`).

- **The Kelvin's side:** while someone is signed in to a Kelvin with their Kelvin account, that Kelvin keeps one WebSocket open to the hub (`/api/relay/kelvin`, with that sign-in's token). It goes out from the lab, so firewalls and Wi-Fi that keeps devices apart (many university, housing and hotel networks) don't stop it. Signing out there, or another person signing in, closes it. The person can switch it off in Settings → **Account**.
- **The phone's side:** the phone signs in with `POST /api/signin` and `"platform": "ios"`, lists the account's Kelvins with `GET /api/relay/kelvins`, and calls Kelvin's phone routes on one through `/api/relay/kelvins/<id>/api/...`. Answers stream back as they come, a chat turn included.
- **Who answers:** the Kelvin, as it would that person's phone reaching it directly, with control (`remote.py`): the same routes and refusals. Settings, the lab config and instruments stay on the computer.
- **Unshared work stays off the hub.** The hub keeps nothing that passes through, but it sees it in passing: there is no end-to-end encryption yet. So Kelvin leaves the person's unshared work out of what the hub carries (unpublished, confidential and model-limited projects, incognito chats, and their runs): lists come without it (with an `X-Kelvin-Hidden` count), and asking for it is refused. The phone sees it when it reaches the Kelvin directly.
- **The lab's state, without unshared work's names.** What keeps the lab safe still goes through the hub: that a run is on and how far it is, **Stop**, alarms and the instruments' values, and that a request waits. Unshared work's names and data don't: the live view of its run (while it runs and after it ended) comes without its plan, run id and points; its approval cards and notices say only that they are unshared work's (**Decline** works from the phone; approve one where you can see it, on the lab network or through Tailscale); its queue items without their names and notes; its watches without their notes; the mounted sample without its name; what Kelvin is doing for it (a recipe, a chat's request) without its name; the runs and queue items a restart cut short, counted but not named; and the lab record's lines and the chats it continued without it. An answer Kelvin can't check for unshared work doesn't go. Each card, notice, queue item, watch and record line keeps whose work it is for this.
- **Directly when possible:** through the hub, the phone asks for a token of its own (`POST /api/phone/direct`), when the Kelvin's phone access is on. It then tries the Kelvin's addresses first (lab network, Tailscale) and goes through the hub only when none answers. `DELETE /api/phone/direct` forgets that token; the app does it when the person signs out.
- **Limits:** a phone's request is at most 1 MB; the Kelvin has 60 seconds to start answering, and an answer may go 15 minutes without a byte; one person has at most 16 requests through the hub at once. A phone's token never opens the Kelvin's side.
- **The hub's memory:** which Kelvins are online is kept in memory. A restarted hub has none, and each Kelvin connects again by itself (within seconds, then backing off to 5 minutes while the hub can't be reached).

On the Kelvin's socket, text frames are JSON: the Kelvin's `{"t": "hello", "name", "version", "direct"}` first; the hub's `{"t": "req", "id", "method", "path", "query", "type", "body" (base64), "phone": {"id", "name"}}` and `{"t": "cancel", "id"}`; the Kelvin's `{"t": "head", "id", "status", "headers"}`, then `{"t": "end", "id"}` or `{"t": "fail", "id", "message"}`. An answer's bytes come as binary frames: the request's id in 4 bytes, then the bytes.

The hub needs the `websockets` package for this (the `hub` extra brings it), and its proxy must pass WebSockets through: Cloudflare Tunnel and Tailscale Funnel do.

## For signed-in Kelvins

Two things the hub does for a Kelvin where someone is signed in with their Kelvin account, so that no Kelvin needs them itself (`hub/drawing.py`; Kelvin's side `web/vision.py` and `web/agent.py`):

- **Which models see pictures:** `GET /api/models/pictures` answers, for each of Kelvin's ready-made services (OpenRouter, OpenCode, DeepSeek, Groq, Mistral, xAI), the models that take pictures and those that don't. The hub reads them from [models.dev](https://models.dev), a public list of models, at most once a day, and keeps them in `models-dev.json`. When models.dev can't be read, the hub keeps what it has and tries again an hour later. Each Kelvin fetches the list once a day ([portal.md](portal.md#in-the-portal)).
- **PDF pages drawn as pictures:** `POST /api/pdf/pages`, for a chat whose model sees pictures but can't read PDFs. Kelvin sends only the pages that model needs (at most 6), as one small PDF, and gets back one PNG per page.
  - **What never goes:** pages from incognito chats or from unpublished, confidential or model-limited projects. Those chats get the pages' text.
  - **Nothing kept:** the hub draws in memory and answers; it keeps and logs nothing of the pages.
  - **Its own process:** `hub/pdfdraw.py` draws with pypdfium2 (PDFium, Chrome's PDF engine) in a separate process, so a broken or hostile PDF can't stop the hub. Limits: 10 MB in, 8 pages, 60 seconds, 1600 pixels on a page's longest side. One person has at most 3 drawings and syncs running at once.
  - **Without pypdfium2:** the hub answers 501, and Kelvin gives the model the pages' text. The `hub` extra brings pypdfium2; the Kelvin app never does.

## Models through the hub

People signed in to Kelvin with their Kelvin account use Claude, GPT, Gemini and OpenCode (Zen, its pay-per-use service, and Go, its subscription: each with its own key) with the keys the owner sets on the hub, so nobody needs keys of their own (`hub/llm.py`; Kelvin's side `web/hubmodels.py` and `web/providers/hub.py`).

- **In Kelvin:** the model picker shows **Claude · Kelvin hub**, **GPT · Kelvin hub**, **Gemini · Kelvin hub**, **OpenCode Zen · Kelvin hub** and **OpenCode Go · Kelvin hub** while the hub has a key for each, and Settings → **Models** shows the month so far. A new chat starts on Claude Opus through the hub when the person has no Claude key of their own.
- **The Kelvin app** (the user version other people install, and the private build: `edition.py`) takes no API keys: its models are the hub's, a model server on the person's own computer, and their ChatGPT plan. Only Kelvin from source, for its developers, also takes the person's own keys and other model services.
- **Each service's own API, unchanged:** `/llm/<service>/<path>` passes a request on and streams the answer back as it comes. Kelvin's model connections only change their address and send the person's hub token as their key, so prompt caching, thinking, pictures, PDFs and web search work as with a key of one's own. Only Kelvin's calls pass: answers, token counts and model lists (`llm.SERVICES`); anything else is 404 and never reaches a service. Google's parameters that reshape an answer (`fields`, `callback`, `alt` other than `alt=sse`) are left off, so its token counts can always be read.
- **Keys:** set, replaced and removed on the owner console's **Models** page. They stay on the hub, sealed with `gateway.key` in its folder, and never go to anyone's computer; the console shows only their last 4 characters. Back up `gateway.key` apart from the database: without it, the keys must be set again.
- **Which models:** each service offers every model it lists, or only those the owner names on the Models page. A model that isn't offered is refused (403).
- **Who may use it:** each service is everyone's, or only the accounts whose emails the owner lists on the Models page; Kelvin doesn't show it to anyone else, and the hub refuses them (403). For a key that is one person's, such as OpenCode Go's single-person plans (Go Teams is per member).
- **OpenCode's sessions:** OpenCode routes and caches by conversation, and OpenCode Go refuses a request without `x-opencode-session`. Kelvin sends one per chat (a hash of it, never the chat's id); the hub passes it on, and for a Kelvin that sends none it sends one per person.
- **What is kept:** for each answer, who, which lab, which service and model, the tokens (prompt, cached, written to cache, answer) and what it cost. Never the text: what a chat says reaches the hub only as Kelvin shares it ([What is shared](#what-is-shared)), never from these calls. Costs use [models.dev](https://models.dev)'s prices, read once a day with the list of models that see pictures (`hub/catalog.py`, `models-dev.json`), with dearer tiers past a context size, Claude's cache writes kept an hour at twice the input price (models.dev's write price is the 5-minute one; Kelvin's chats ask for the hour), GPT's cache writes at their own price, and Claude's web searches at $0.01 each; a model models.dev doesn't list counts at $15 in and $75 out per million tokens. The services' own bills are what the owner pays.
- **Budgets:** each person has a monthly budget in dollars: $20 until the owner sets another default, or their own on their page in the console. A lab can have one too (with labs switched on). A request past either is refused (402) with what to do, which Kelvin shows in the chat. Months are UTC calendar months.
- **The 80% mail:** an answer that takes a person past 80% of their budget sends them a mail, once a month.
- **Limits:** per person, 60 requests a minute and 4 answers at once; 40 MB a request. `"llm": {"per_minute", "at_once", "alert_at", "fallback_price"}` in `hub.json` changes them.
- **Confidential work never goes through the hub:** Kelvin refuses the hub's models in confidential projects and in chats that left one. Other chats (unpublished projects and incognito chats too) pass through the hub to the model's service; the hub keeps none of their text from these calls. The terms and privacy notice say so (version 2026-10-29, and 2026-11-08 with sharing).
- **What a person sees when it fails:** a sign-in that ended (401: sign out and in again), a budget used up (402), a model not offered or new terms (403), too many at once (429), the service refusing the hub's key or out of reach (502: tell the hub's owner), no key on the hub for that service (503).

### Moving the hub to a server

Model traffic for many labs is more than Tailscale Funnel carries, so the hub moves to a server before other labs use these models. Nothing in the hub changes:

1. **A server:** any always-on Linux machine. A small cloud VM (2 CPUs, 2–4 GB, 40 GB disk) carries thousands of people; answers stream through it, so its network matters more than its CPU.
2. **Install:** Python 3.10 or newer, a checkout of this repository, and `pip install -e ".[hub]"`.
3. **Move the folder:** stop the hub, run `kelvin-hub backup`, and copy the whole hub folder (`hub.sqlite3`, `hub.json`, `gateway.key`, `files/`) to the server.
4. **Run it as a service:** a systemd unit that runs `kelvin-hub --home /srv/kelvin-hub` as its own user and restarts it. It still listens on `127.0.0.1:8780` only.
5. **Put it online:** Cloudflare Tunnel (or Caddy with a domain name) to `127.0.0.1:8780`, then `--base-url https://hub.example.org` and, behind Cloudflare, `"client_ip_header": "CF-Connecting-IP"` ([Putting it online](#putting-it-online)).
6. **Point Kelvin at it:** an update of Kelvin (`central.DEFAULT_HUB`) and the iPhone app (`Account.defaultHub`) changes the built-in address. Until people have that update, keep the old address answering.

## Sharing from Kelvin

Kelvin shares over the person's Kelvin account sign-in ([Signing in to Kelvin](#signing-in-to-kelvin)): it syncs with the token `/api/signin` gave this computer for them, at the hub they signed in to. Sharing has no connecting of its own.

- **Why none of its own:** Kelvin 0.3.9 to 0.3.12 connected through the hub's `/connect` page (a PKCE code traded for a token per installation and lab). At that trade the hub folds the Kelvin's earlier ids into the new one and ends their tokens. One of those is the bare computer id, which the Kelvin sign-in uses, so connecting ended the person's sign-in on that computer. The hub still takes such connections from those versions. Kelvin no longer makes them, and forgets what one kept (`hub_url`, `hub_token` and `hub_installations` in the person's settings).
- **Where it goes:** the sign-in's token is one per account and computer (its installation id, `hub_installation` in `~/Kelvin/settings.json`), with no lab. What a Kelvin shares is filed under that account and computer.
- **A new token at each sign-in:** signing in again replaces the token on the hub. Kelvin syncs with the new one at once, and a sync still going with the old one stops before its next request. A 401 never makes Kelvin forget the token, which is the sign-in's: the status says to sign out and in again.

`HubClient` in `hub/client.py` does Kelvin's side: `push` and `me` with the sign-in's token, plus `collect(store)`, which gathers what a `ChatStore` may share. Its `begin`, `finish`, `upload` and `disconnect` belong to the earlier connection, which Kelvin no longer makes.

### In the Kelvin app

Settings → **Account** (`web/hubsync.py`, routes in `web/app.py`), while someone is signed in with a Kelvin account:

- **The switch:** **Share my chats and run records with the Kelvin team** (`hub_share` in the person's `settings.json`, off unless `on`), in the card at the top of the page, under the phone's switch. It acts at once. Before Kelvin 0.3.51 it was on unless `off`: for a person who never chose, the update switches it off, and Kelvin takes back what it shared.
  - **Off** takes back from the hub what this Kelvin shared: the next syncs send everything in `hub-sync.json` as `removed` until nothing is left, then send nothing at all. The status says what is still there; **Take back now** does it at once.
  - **On again** shares everything once more.
- **The hub's terms:** the hub refuses a sync until the person has agreed to its current terms (403, with `consent_url`).
  - **The bar:** while sharing is on, a bar in the portal's notice row (`hub_terms` in `GET /api/state`) says the terms changed and Kelvin shares once you agree, with **Read and agree** (the terms dialog, here in Kelvin) and **Switch it off** (Settings → **Account**). The sign-in's `needs_consent` shows it before any sync. Otherwise it shows at the first sync the hub refuses: a Kelvin with nothing new to send learns of new terms when something changes (a chat's next turn), and sends nothing meanwhile.
  - **Before agreeing at all:** Kelvin's own **Create a Kelvin account** has the box that agrees to the hub's terms and privacy notice, and nothing beside it: those say what Kelvin shares with the Kelvin team once sharing is switched on, and what never goes.
  - **Agreeing in Kelvin:** signing in when the person's agreement is to older terms, the sign-in screen has a step with the terms (which say what changed) and the privacy notice to read (the hub's pages, which anyone reads signed in or not), **I agree**, **Agree** and **Not now**. The bar's **Read and agree**, Settings → **Account** (**Read and agree to the new terms**) and Settings → **Models** (while the hub's models wait) open the same in a dialog. Kelvin sends the version it showed with the token of its sign-in (`POST /api/hub/terms`, then the hub's `POST /api/consent`, whose audit says `in: kelvin`), clears the bar and syncs. Terms that changed again meanwhile are shown before anything is agreed to (409).
  - **An older hub** says no version: Kelvin opens its `/consent` page in the browser, asks the hub every few seconds (`GET /api/hub`), and syncs once the terms are agreed.
- **On the Kelvin hub:** who the person is on the hub, what of theirs is there (`on_hub`: chats and run records), the last sync, **Your account on the hub**, **Sync now** and **Also share older runs**.
- **Background sync:** every two minutes, at once after a sign-in or a change of the switch, and on **Sync now**. Kelvin sends what changed with `POST /api/sync`, projects first, in batches of at most 500 items or 16 MB, and what is gone since (`removed`). When nothing changed, it sends nothing.
  - **What goes:** what `collect` returns: each chat with its marks and rules, and shared projects with theirs. Plus the records of the finished runs in the person's data folder (those with a `meta.json`) in a shared project or in none: each run's `meta`, with its card as `meta.card` (`rundata.card`: its plan, what it swept, measured and held, its results, assessment and issues), never its files (`run.h5`, plots, reports).
  - **The card:** read from `run.h5`, where it is kept. It goes while the record stays within what the hub keeps of a run (256 kB of `meta`): a card that doesn't fit, or can't be read, is left out, never the run. A fit or re-analysis after the run changes its card, so the record goes again.
  - **Whose runs:** only runs this person made (`meta.json` notes `user`), so a data folder shared with someone else never sends their runs.
  - **Runs that stay:** a run's `meta.json` says when an incognito chat started it (`incognito`), or a chat that keeps an unshared project's marks or limits (`unshared`, whatever project the run is filed under). Neither is ever sent.
  - **Older runs:** some runs are held back: those made before Kelvin noted who made them, those made without accounts, and those of queue items from before (`legacy`). They go only when the person ticks **Also share older runs** (`hub_old_runs`, off by default). The status says how many stay back.
  - **A hub that takes no shared work:** a 404 (an older hub, or one with sharing off) is said quietly ("The hub doesn't take shared work yet") and tried again at the next sync.
- **Without a Kelvin account** (accounts on this computer only, or Kelvin without accounts), Kelvin shares nothing, and the section says sharing needs a Kelvin account.
- **Marks on chats** (`web/store.py`):
  - **Leaving a project:** a chat moved out of an unpublished, confidential or model-limited project, or left when the project is deleted, keeps that mark and the project's limits as they are then. Changing a project's limits locks no chat. A chat keeps limits only when it runs under them, leaves the project or is left when it is deleted.
  - **Notes:** a chat in unshared work keeps no notes for every conversation.
  - **Runs:** its runs are filed in its own project, without another project's mounted sample.
- **What was sent** is kept in `hub-sync.json` in the person's folder (`~/Kelvin/users/<name>/`): ids and fingerprints, never content, and a digest of the hub account and computer it went to (never the token). A new sign-in sends only what changed; another account or computer gets everything once more.
- **Safety:**
  - **Snapshot:** a sync works from a snapshot of the person's sign-in, settings, chats and data folder. It stops when someone else signs in, or the person signs in again or flips the switch, so work never goes out under another's token. An old token's 401 after such a change ends only that sync.
  - **Redirects:** Kelvin never follows a redirect from the hub, so the token can't be sent on to another address.
  - **Unreachable:** Kelvin tries again at the next sync.

| Portal call | What it does |
|---|---|
| `GET /api/hub` | `{"signed_in", "sharing", "url", "account", "problem", "waiting", "sync", "synced_at", "syncing", "on_hub", "taking_back"}`, never the token |
| `POST /api/hub/sync` | Sync now; switched off, take back now |
| `POST /api/settings` | `{"hub_share": "on"}` or `"off"`; `{"hub_old_runs": "on"}` or `"off"` |
| `POST /api/hub/terms` | `{"version"}`: the person agrees to the hub's terms of that version (the one shown), with the token of their sign-in → `{"agreed"}`; 409 with `terms` when they changed again, 404 on an older hub |
| `GET /api/state` | `hub_terms`: `{"consent_url", "consent_version", "terms_url", "privacy_url"}` while the hub waits for its new terms, else `null` |

## Running it

From a checkout with the `hub` extra installed (the `web` extra runs it too, but without drawing PDF pages):

```bash
.venv/Scripts/kelvin-hub --home C:/path/to/hub-data
```

It serves `http://127.0.0.1:8780` on this computer only. On the first start it writes the operator key to `operator-key.txt` in that folder, and never prints it. Other commands:

```bash
.venv/Scripts/kelvin-hub backup --home C:/path/to/hub-data
```

```bash
.venv/Scripts/kelvin-hub operator-key --home C:/path/to/hub-data
```

```bash
.venv/Scripts/kelvin-hub stats --home C:/path/to/hub-data
```

`backup` copies the database now and `stats` prints the counts. `operator-key` makes a new operator key; the old one stops working at once, even while the hub runs, and so do the operator pages opened with it.

While the hub runs, it looks at `hub.json` every 30 seconds and takes it in again when it changed. If the file can't be read at that moment (half saved, or between an editor's delete and rename), it keeps the settings it has and tries again later. It never writes defaults over it. `KELVIN_HUB_HOME` can stand in for `--home`.

The hub's folder:

| Path | What it is |
|---|---|
| `hub.sqlite3` | Accounts, labs, connected Kelvins and what they share (SQLite, WAL). An older database is brought up to date when the hub starts (schema 3) |
| `hub.json` | Settings: `base_url`, `consent_version`, `require_confirmed_email`, `sharing` (true), `labs` (false), `run_files` (false), `contact` (unset), `max_file_mb` (200), `quota_gb_per_user` (5), `backups_kept` (30), `client_ip_header`, `mail`, and the operator key's hash. No secrets. The hub writes it with the defaults at its first start, so one written by an older hub keeps that hub's: `"sharing": false` and `"consent_version": "2026-10-29"` |
| `operator-key.txt` | The operator key, written once. Move it to a password manager, then delete the file |
| `files/<user_id>/<run_id>/` | Run files, while `run_files` is on (or from before) |
| `backups/hub-YYYY-MM-DD.sqlite3` | One copy of the database a day, made while the hub runs (SQLite's online backup) |
| `outbox/` | Mail the hub would have sent, while no SMTP server is set |
| `terms.html`, `privacy.html` | Optional: your own terms and privacy notice, replacing the built-in drafts |
| `hub.log` | Sign-ins, connections, syncs and errors by account number; never addresses, passwords or tokens |
| `models-dev.json` | Which models see pictures and what they cost, from models.dev, read again once a day |
| `gateway.key` | Seals the model services' keys in the database ([Models through the hub](#models-through-the-hub)). Back it up apart from the database |

**Backups:** the daily copy covers the database only. Back up `files/` with the rest of the machine.

## The owner console

`/admin` asks for the operator key. It is the Kelvin team's, not an account or a role. No user sees any of this.
- **The hub:** people, confirmed, disabled, active in 30 days, computers, sign-ins, the database's size and the last backup. **Back up the database now** makes a backup at once.
- **Models** (`/admin/models`): each model service's key (set, replace, remove; only its last 4 characters shown), the models it offers, the default monthly budget, and the month's use: by person (requests, tokens, cost, budget, share used), by model and by lab, with a CSV of it. A person's page shows their month and takes their own budget ([Models through the hub](#models-through-the-hub)).
- **People:** everyone, newest first, 200 to a page (**Older** and **Newer**), and searchable by name or email. Each row shows whether the email is confirmed, how many computers the person is signed in on, the Kelvin version they last used, when they joined and when they were last seen (and their labs, while `labs` is on). **Disable** stops an account from signing in and ends its sign-ins on every computer; **Enable** undoes it.
- **Download everyone (CSV):** `kelvin-people.csv`, one row per person: id, name, email, ORCID iD, email confirmed, joined, last seen, disabled, computers, Kelvin version, terms agreed. It never includes a password or its hash, nor anything anyone shared.
- **Latest shared chats** (while sharing is on): the 50 most recently updated across everyone, each with its person, project and size, linked to its page.
- **A person's page** (`/admin/users/<id>`, from their name): the account (with the terms they agreed to, and whether those are the ones in force), the computers they sign in to Kelvin on (name, system, Kelvin version, first sign-in, last used), their shared work, and the hub's record of the account (sign-ins, changes, resets, with IP addresses), newest first.
  - **Shared work:** how many projects, chats and runs; then the projects (with how many chats and runs each), the chats (title, project, computer, last updated, size) and the runs (plan, status, started, device, project), newest first, the latest 200 of each, each chat and run linked to its own page.
- **A chat's page** (`/admin/users/<id>/chats/<chat>`): the conversation, read-only.
  - **Turns:** each one's words.
  - **Folded:** a model's thinking, tool calls and tool results. A result is cut past 20,000 characters and a call's input past 4,000, saying how much more there is.
  - **Files:** pictures and attached files are named, never shown.
  - **Escaped:** a chat's text is shown as text: markup in it never runs.
  - **Download it (JSON)** (`…/json`) gives the chat as Kelvin sent it.
- **A run's page** (`/admin/users/<id>/runs/<run>`): its record (`meta`) laid out, then whole as JSON.
- **The person's record:** each opening or download of a chat or a run goes in the hub's record of their account, so their **Export** shows it.
  - **Identified by its own id** (Kelvin's chat or run id), never its title, which `hub.log` would then keep.
  - **No address:** the operator's IP address isn't put on it.
  - **Lists:** reading the lists of shared work isn't recorded.
- **Recent activity:** the last 60 entries of the hub's record, each linked to its account.

`GET /api/admin/stats` with an `X-Operator-Key` header gives the counts as JSON.

## Putting it online

1. Run it on an always-on machine, still on `127.0.0.1`.
2. Reach it through [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (free https, no open ports) or a university hostname.
3. Start it with `--base-url https://hub.example.org`, which is saved in `hub.json`. Links in mails use this address, and the hub answers only to it and to `127.0.0.1`/`localhost`. Behind Cloudflare, also set `"client_ip_header": "CF-Connecting-IP"`, so sign-in limits and the record see real addresses; behind Tailscale Funnel (or `tailscale serve`), `"client_ip_header": "X-Forwarded-For"`, which `tailscaled` sets to the client's address. Without it, everyone there counts as one address, so ten wrong sign-ins from anyone hold off every sign-in for 15 minutes. The hub believes that header only from a proxy on its own computer (`cloudflared`, `tailscaled`), and takes its last value.
4. Set `mail` in `hub.json` (`smtp_host`, `smtp_port`, `starttls`, `username`, `from`) and the password in the `KELVIN_HUB_SMTP_PASSWORD` environment variable. Then turn on `require_confirmed_email`.
5. Replace the draft terms and privacy notice (`terms.html`, `privacy.html`), and raise `consent_version` (a date, YYYY-MM-DD) whenever they change, or when you switch `labs` or `run_files`: the built-in ones change with those.

Kelvin accepts a hub address on the internet only with https.

## Safety

- **Pages:** no JavaScript, no framing, and a Content-Security-Policy on every response. Forms carry an anti-forgery value, and posts from other sites are refused.
- **Files:** run files are always downloaded, never shown in the browser. A chat's pictures and attachments never reach the hub; their names are shown as text.
- **Rate limits:** kept in memory, so a restart resets them. An IPv6 address counts by its /64. These are all of them:

  | What | Limit |
  |---|---|
  | Signing in on the hub's pages, and the password on the connect page | 10 failures in 15 minutes, per email and per address |
  | Signing in to Kelvin (`/api/signin`) | 10 failures in 15 minutes, per email and computer (its installation id) and per address, so strangers elsewhere can't lock a person out of their own computer; past 30 failures an hour for an email, only computers the account is signed in on still try |
  | New accounts | 20 an hour per address |
  | Reset mails | 20 an hour per address |
  | Token trades | 30 failures in 15 minutes per address |
  | Operator key | 5 failures in 15 minutes per address |
  | Exports | one per person per 10 minutes |
  | Syncs and uploads | 3 of one person's at once; more get a 429 at once. Each waits at most 10 seconds for that person's other sync or upload, then gets a 429 |

  Account-page forms have none. One person's flood of syncs or uploads never takes the worker threads from anyone else, and all syncs and uploads together hold at most 16 of the hub's 40 worker threads (the rest wait their turn without one), so pages and sign-ins always have threads left. A sync's JSON is read in a worker thread, never on the event loop.
- **Request bodies** are capped as they stream in: forms at 64 kB, token trades at 16 kB, one sync at 64 MB.
- **Uploads:**
  - refused (403) while `run_files` is off;
  - names are checked part by part (no `..`, and no device names, even with a space as in `NUL .txt`);
  - each file needs a `Content-Length` and may be at most `max_file_mb`;
  - its bytes are held against the person's share (`quota_gb_per_user`, which chats count toward too) before it streams in, so parallel uploads can't overrun the share. The share is summed from each chat's stored size, never by reading the chats;
  - at most 2 uploads at a time per person.
- **Times** Kelvin sends must be numbers from the year 2000 to a day from now; anything else is stored as the moment it arrived.

## API

| Call | What it does |
|---|---|
| `GET /api/health` | `{"ok": true, "hub": version, "draws_pdfs"}` |
| `GET /api/llm` | A signed-in Kelvin's: the model services offered `{"services": [{"id", "name", "models"}], "month", "spent", "budget", "renews", "lab"}` |
| `GET, POST /llm/<service>/<path>` | A signed-in Kelvin's call to a model service (`anthropic`, `openai`, `gemini`, `opencode`, `opencode-go`), passed on with the hub's key; its token in `x-api-key`, `x-goog-api-key` or `Authorization: Bearer`. Errors in the services' own shape: 401, 402, 403, 404, 413, 429, 502, 503 |
| `GET /api/models/pictures` | A signed-in Kelvin's: `{"at", "services": {"opencode": {"yes": [...], "no": [...]}, ...}}` |
| `POST /api/pdf/pages` | A signed-in Kelvin's: PDF pages as the raw body → `{"pages": [{"mime": "image/png", "data"}]}`. 403 until new terms are agreed to, 413 over 10 MB, 422 when they can't be drawn, 429 with 3 already running, 501 without pypdfium2 |
| `POST /api/signin` | Kelvin's sign-in: `{"email", "password", "installation"}` → `{"access_token", "user", "needs_consent", "account_url"}`, and the terms in force while `needs_consent` |
| `POST /api/consent` | A signed-in Kelvin's person agrees, in Kelvin, to the terms in force: `{"version"}` (the version Kelvin showed them) → `{"agreed"}`; 409 with the terms in force when they changed again since |
| `POST /api/register` | Kelvin's Create account: `{"name", "email", "password", "consent": true, "orcid"?, "installation"}` → as `signin` |
| `POST /api/reset` | `{"email"}`: mail a reset link → always `{"ok": true}` |
| `POST /api/password` | `{"old", "new"}`: change the password |
| `POST /api/signout` | End this Kelvin's or phone's sign-in: its token stops working (204) |
| `POST /api/account/delete` | Delete the account from Kelvin or the iPhone app: `{email, password}`, the same as **Delete account** on the account page; the same lockouts as signing in |
| `WS /api/relay/kelvin` | A signed-in Kelvin's connection for its person's phones (see Phones through the hub) |
| `GET /api/relay/kelvins` | The account's Kelvins: `{"kelvins": [{"id", "name", "platform", "version", "online", "direct", "last_seen"}], "user"}` |
| `* /api/relay/kelvins/{id}/api/...` | A phone's call to one of them, passed through. 503 with `offline: true` when it isn't online |
| `POST /api/token` | `{"grant_type": "authorization_code", "code", "code_verifier", "redirect_uri"}` → `{"access_token", "user", "lab"}` |
| `DELETE /api/token` | Disconnect this Kelvin |
| `GET /api/me` | The person, their labs (while `labs` is on), the lab this Kelvin shares into, `needs_consent`, `consent_url`, `account_url`, `run_files` (whether the hub takes run files), and the terms in force (`consent_version`, `terms_url`, `privacy_url`) |
| `POST /api/sync` | `{"projects": [...], "chats": [...], "runs": [...], "removed": {"projects", "chats", "runs"}}`, at most 500 of each → counts, plus what was refused and why. 413 over 64 MB. Before the person agrees to new terms, 403 with `needs_consent` and `consent_url`, unless it only removes (one over 256 kB is refused unread) |
| `PUT /api/sync/runs/{run}/files/{name}` | One run file, as the raw body with a `Content-Length` (send the run first). Errors: 403 with `run_files: false` while the hub keeps runs' records only, 411 without a length, 413 over the size or the person's share, 429 with 2 uploads already running |

Every call but `health`, `token`, `signin`, `register` and `reset` needs `Authorization: Bearer <token>`: the token of Kelvin's own sign-in (`/api/signin`), or of an older Kelvin's connection (`/api/token`). Items are keyed by the Kelvin's own ids:
- projects: `id`, `name`, and `shared`, `unpublished`, `confidential`, `rules` as in Kelvin;
- chats: `id`, `project`, `title`, `messages` (up to 5 MB), `updated`, and `incognito`, `shared`, `unpublished`, `confidential`, `rules`;
- runs: `id`, `project`, `device`, `meta` (up to 256 kB), `created`, and `incognito`, `unpublished`, `confidential` (also read from `meta`).

## Known limits

- **New projects:** a new project's name and chats go up within two minutes, before it can be marked unpublished. Marking it later deletes them from the hub.
- **Backups:** they keep deleted work for up to 30 days.
- **Phones:** a phone reaching Kelvin directly can set a project back to Shared.
- **Through the hub:** the hub can read what it passes between a phone and a Kelvin; unshared work never goes that way, but the rest does, in passing.
- **Registration:** it says when an email already has an account.
- **Quoting:** Kelvin's tools never read an unshared run into another chat ([data.md](data.md#your-data)), but a person can still paste it there. The chat then carries it to the hub.
- **Each sync** looks through everything for what changed.
- **Who opened it:** the operator key is the Kelvin team's, not one person's, so the record says the team opened a chat, not who in it.
- **Earlier connections:** what a Kelvin shared through `/connect` (Kelvin 0.3.9 to 0.3.12) stays under that connection's installation id. Kelvin's sign-in shares under the computer's own id, so work both shared is on the hub twice; the old copy goes only with the account (**Delete account**).

## Not built yet

- Seeing labmates' shared work from inside Kelvin (labs are off). With labs on, it is on the hub's account page.
- A person deleting one shared chat or run on the hub itself. Deleting it in Kelvin removes it, as **Delete account** removes everything.
- Removing one file of a shared run from the hub. A run removed in Kelvin goes with all its files.
- Signing in with ORCID or Google (the `external_identity` column is there for it).
- Changing one's email address.
- The hub's permanent server (it runs on a lab workstation through Tailscale Funnel for now), and its mail server.
- End-to-end encryption between a phone and a Kelvin through the hub, and push notifications (APNs) through it.
