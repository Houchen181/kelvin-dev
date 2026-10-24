# The Kelvin hub

The hub is the Kelvin team's server for **Kelvin accounts**: one email and password that signs a person in to Kelvin on every computer ([portal.md](portal.md#kelvin-accounts-one-account-every-computer)). It is the owners' tool, not a place for users: the **owner console** (`/admin`) shows everyone's account, the computers each signs in on, and what happened to it. It is built for a few thousand people (about 5,000): one SQLite file.

**What users see:** only what an account needs. That is creating one (in Kelvin, or on the hub), the email confirmation and password reset links, and a short account page: name and ORCID iD, password, export and delete. The hub keeps no one's work, and no user sees another.

**Sharing, switched off:** the hub can also keep people's shared work: labs with join codes, Kelvins connected to share into them, shared projects, chats and runs. That is built and tested but off, on both sides:
- On the hub, `"sharing": false` in `hub.json` (the default). Those pages and API calls answer 404, and the account page and owner console leave them out.
- In Kelvin, `HUB_SHARING = False` in `web/app.py`.

The sections below on sharing describe it for when it is switched on.

**Status:**
- **Built and tested:** the server, its database, a Kelvin's client (`hub/client.py`), Kelvin's sign-in with Kelvin accounts (`central.py`), and sharing (Settings → **Account** → Kelvin hub, and syncing in the background: `web/hubsync.py`; switched off).
- **Drafts:** the terms and privacy notice.
- **To do:** putting the hub on the internet for good. Kelvin's built-in hub address (`central.DEFAULT_HUB`) stays empty until then.

## What is shared

- **Shared by default:** normal projects and unfiled chats, with their runs and whole conversations.
- **Never sent:**
  - projects marked unpublished or confidential, and projects whose **Who may see this project** limits the models or literature search;
  - incognito chats, and chats that ran under such limits;
  - API keys, and ChatGPT or Claude Code sign-ins.

  Kelvin's client (`collect` in `hub/client.py`) leaves these out, and sends an unshared project only as its id and "not shared", never its name.

  The hub checks the marks again on what arrives:
  - it refuses chats marked incognito, unpublished, confidential or not shared, or with model limits;
  - it refuses runs marked incognito, unpublished or confidential, in the run or its `meta`;
  - it refuses chats and runs in a project that isn't shared, and deletes any copy it had of them;
  - when a project stops being shared, it deletes everything it had of it.

  It can only check the marks Kelvin sends: an unmarked chat or run is taken as shared.
- **Marking a project:** in Kelvin, a project's **Project settings → On the Kelvin hub** is *Shared* (the default), *Unpublished* or *Confidential*. A chat moved out of an unpublished, confidential or model-limited project, or left unfiled when that project is deleted, keeps the mark (or the project's model limits) and stays unshared. Changing a project's limits locks none of its chats. A run started (or queued) from an incognito chat, or from a chat that keeps an unshared project's marks or limits, says so in its `meta.json`, and is never sent. Nor is a run whose project is gone, since Kelvin can't tell whether that project was shared.
- **Who sees it:** its owner, and the other members of the lab it was shared in, while both are members. A Kelvin connected with "No lab" shares only with its person.
- **Which lab:** everything a Kelvin shared is filed under the lab it shares into now. Connecting it again with another lab, or with "No lab", moves all of it at once.
- **Leaving a lab** takes your work out of it for good: joining again doesn't bring it back, only connecting a Kelvin into that lab again does.

## Accounts and labs

- **Registering:** name, email, an optional ORCID iD (its check digit is checked) and a password of 8 characters or more. Agreeing to the terms and the privacy notice is required, and the hub records which version and when. When the version in `hub.json` changes, everyone is asked to agree again before going on, and syncing pauses until they do.
- **Passwords:** PBKDF2-SHA256 with 600,000 iterations, as OWASP suggests.
  - **Older hashes:** a hash made with fewer is redone at the person's next sign-in, unless the password changed meanwhile.
  - **Timing:** checking it takes as long as a current one, as does checking an email that has no account. Browser sessions, app tokens and one-use codes are stored only as their SHA-256. A one-use code works once, even when two requests bring it at the same moment.
- **Email:** the hub sends a confirmation link, and password reset links. Until an SMTP server is set, or when sending fails, these go to `outbox/` in the hub's folder for the operator to pass on. `"require_confirmed_email": true` then stops unconfirmed people from connecting a Kelvin or sharing.
- **Resetting a password:** the reset page gives the same answer, and as fast, whether or not the account exists; the mail goes out after the answer. A new password that won't do leaves the link working. A reset signs out every browser and disconnects every Kelvin of the account. A password change in the account page signs out the other browsers but keeps the Kelvins connected.
- **Labs have no owner.** Anyone can make a lab. Everyone in it sees its join code (like `K7Q2-9XMB-4TRA`) and can pass it on. A lab nobody is in any more is deleted.
- **Your data:** **Export everything** on the account page downloads a zip of the account, labs, connections, everything shared and the hub's record of the account's activity. It can be made once every 10 minutes.
- **Deleting an account:** **Delete account** (with the password) deletes the account and all it shared, even if the person hasn't agreed to new terms. Its files are deleted after the database is updated, so a large account never holds up anyone else's sign-in or sync.
  - **Also removed:** the Kelvins only that account used, and mail to it waiting in `outbox/`.
  - **Timing:** it waits for the person's sync or upload in progress, so no file lands afterwards.
  - **The log:** `hub.log` never has addresses. The activity record keeps only that account number N was deleted, without addresses or details. Account numbers are never reused. Database backups keep a deleted account until they age out (30 days by default).

## Signing in to Kelvin

Kelvin's sign-in screen talks to the hub with JSON (`central.py`; routes `api_signin`, `api_register`, `api_reset` and `api_password` in `hub/server.py`):

- **`POST /api/signin`** `{email, password, installation: {id, name, platform, version}}` returns `{access_token, user: {name, email, email_confirmed}, needs_consent, account_url}`.
  - **The installation's shape:** an object whose `id` is the Kelvin's installation id, for example `{"id": "urn:uuid:…", "name": "Lab PC", "platform": "win32", "version": "0.3.12"}`. This differs from `/connect`, whose query sends the id as `installation=`. Without an `id` the hub answers 400.
  - **The token:** one per account and installation; signing in again replaces it. Kelvin keeps it in the person's settings, encrypted with their password, to change the password later.
  - **The installation** is recorded as a connected Kelvin, without a lab, so the account page lists the computers.
  - **Limits:** a wrong password is 401, and counts toward the same limits as the sign-in page (10 per email or address in 15 minutes, then 429). A disabled account is 403. So is an unconfirmed one, once `require_confirmed_email` is on.
- **`POST /api/register`** `{name, email, password, consent: true, orcid?, installation}` makes the account, mails the confirmation link, and signs in as above. When the hub wants the address confirmed first, it answers 403 with `registered: true`. Registrations count toward the same limit as the registration page.
- **`POST /api/reset`** `{email}` mails a reset link and always answers `{ok: true}`, so nobody can probe for accounts.
- **`POST /api/password`** `{old, new}`, with the token, changes the password. Kelvins stay signed in, as with a change on the account page. A reset by mail ends every token.

Kelvin, for its part:
- **Unreachable:** a network error, a 404, any 5xx (such as a tunnel's 530), or an answer that isn't JSON means the hub can't be reached now. Kelvin then checks the password against the hash it kept at the person's last sign-in on this computer. A hub that answers no (401, 403, 429) is never overruled by that hash.
- **Wrong passwords** wait a second before Kelvin answers.

## Phones through the hub

A phone signed in to a person's Kelvin account reaches every Kelvin where that account is signed in, through the hub, with no pairing (`hub/relay.py`, and Kelvin's side `web/relay.py`).

- **The Kelvin's side:** while someone is signed in to a Kelvin with their Kelvin account, that Kelvin keeps one WebSocket open to the hub (`/api/relay/kelvin`, with that sign-in's token). It goes out from the lab, so firewalls and Wi-Fi that keeps devices apart (many university, housing and hotel networks) don't stop it. Signing out there, or another person signing in, closes it. The person can switch it off in Settings → **Account**.
- **The phone's side:** the phone signs in with `POST /api/signin` and `"platform": "ios"`, lists the account's Kelvins with `GET /api/relay/kelvins`, and calls Kelvin's phone routes on one through `/api/relay/kelvins/<id>/api/...`. Answers stream back as they come, a chat turn included.
- **Who answers:** the Kelvin, as it would a paired phone of that person with control (`remote.py`): the same routes and refusals. Settings, the lab config, instruments and pairing stay on the computer.
- **Unshared work stays off the hub.** The hub keeps nothing that passes through, but it sees it in passing: there is no end-to-end encryption yet. So Kelvin leaves the person's unshared work out of what the hub carries (unpublished, confidential and model-limited projects, incognito chats, and their runs): lists come without it (with an `X-Kelvin-Hidden` count), and asking for it is refused. The phone sees it when it reaches the Kelvin directly.
- **Directly when possible:** through the hub, the phone asks for a token of its own (`POST /api/phone/direct`), when the Kelvin's phone access is on. It then tries the Kelvin's addresses first (lab network, Tailscale) and goes through the hub only when none answers. `DELETE /api/phone/direct` forgets that token; the app does it when the person signs out.
- **Limits:** a phone's request is at most 1 MB; the Kelvin has 60 seconds to start answering, and an answer may go 15 minutes without a byte; one person has at most 16 requests through the hub at once. A phone's token never opens the Kelvin's side.
- **The hub's memory:** which Kelvins are online is kept in memory. A restarted hub has none, and each Kelvin connects again by itself (within seconds, then backing off to 5 minutes while the hub can't be reached).

On the Kelvin's socket, text frames are JSON: the Kelvin's `{"t": "hello", "name", "version", "direct"}` first; the hub's `{"t": "req", "id", "method", "path", "query", "type", "body" (base64), "phone": {"id", "name"}}` and `{"t": "cancel", "id"}`; the Kelvin's `{"t": "head", "id", "status", "headers"}`, then `{"t": "end", "id"}` or `{"t": "fail", "id", "message"}`. An answer's bytes come as binary frames: the request's id in 4 bytes, then the bytes.

The hub needs the `websockets` package for this (the `hub` extra brings it), and its proxy must pass WebSockets through: Cloudflare Tunnel and Tailscale Funnel do.

## Connecting a Kelvin

Sharing work, switched off for now. This works like Kelvin's Sign in with ChatGPT, with the hub as the sign-in page:

1. Kelvin opens `<hub>/connect` in the browser, with a PKCE challenge (S256), a state, its own installation id, name, platform and version, and its loopback callback `http://127.0.0.1:<port>/...`. The hub sends the browser back to nothing else.
2. The person signs in, chooses which lab the Kelvin shares into, types their hub password, and presses **Connect**.
   - **Why the password:** it's asked even when the browser is already signed in. On a shared computer, that session may be someone else's.
   - **Who is shown:** the page names the account it would connect to, and the person using Kelvin (the optional `person` parameter Kelvin sends).
   - **Not you?** This signs out and comes back to the request.
   - **Limits:** the password here counts toward the same limit as signing in, so 10 wrong passwords, here or on the sign-in page, also block connecting for 15 minutes.
3. The hub sends the browser back to the callback with a one-use code (10 minutes). Kelvin trades the code and its PKCE verifier for a token at `POST /api/token`. The code is used up together with the connection: if the hub is busy (429) Kelvin can try the same code again; a wrong verifier or callback uses it up.
4. Kelvin keeps the token in the person's own settings, encrypted with their password. The hub keeps one token per account and installation id, so connecting the same Kelvin again replaces it. **Disconnect** on the account page, `DELETE /api/token`, or a password reset ends it.
   - **Earlier ids:** Kelvin may name up to 5 earlier installation ids of its own (`previous`, comma-separated). The hub moves that account's projects, chats, runs and run files from those ids to the new one, keeps one copy where both have an item, and ends the old ids' connections. Where the new id has marked a project not shared since, the old ids' chats and runs of it are deleted, never moved into it.
   - **Ids from before:** the new id's computer part (what Kelvin used before ids were per person) counts as an earlier id without being named.

`HubClient` in `hub/client.py` does Kelvin's side: `begin`, `finish`, `push`, `upload` and `disconnect`, plus `collect(store)`, which gathers what a `ChatStore` may share.

### In the Kelvin app

Settings → **Account** → Kelvin hub (`web/hubsync.py`, routes in `web/app.py`):

- **Connect:** the person types the hub's address and presses **Connect**. Kelvin opens the hub's `/connect` page in the browser, with its callback `http://127.0.0.1:<port>/hub/callback`.
  - **Installation id:** one per person on this computer, so two people on one PC each keep their own connection. It's the computer's id (`hub_installation` in `~/Kelvin/settings.json`, made once), a colon, and the first 12 hex digits of the SHA-256 of the person's folder. The folder is resolved first (`Path.resolve()`, then `normcase`, lower case on Windows), so another spelling of it, such as an 8.3 short name, gives the same id.
  - **Earlier ids (`previous`):** Kelvin remembers the ids each person connected with (`hub_installations` in their `settings.json`, the latest 5). On connect it sends them, plus the id that Kelvin's first hub branch made from the folder as spelled and the bare computer id, at most 5 in all. At the token trade the hub moves what was shared under those to the new id and ends their tokens, so it keeps one copy of everything.
  - **Name:** Kelvin also sends the person's name (`person`), which the hub shows.
  - **Callback:** works only on Kelvin's own computer, never from a paired phone. It answers at once, even while a sync of the old connection is still uploading.
  - **Token:** the hub's address and the token go in the person's `settings.json` (`hub_url`, `hub_token`). The token is encrypted with their password like the API keys, and never reaches the browser or the logs.
- **Status:** who the person is on the hub, which lab this Kelvin shares into, and the last sync of this connection (from `GET /api/me`). If the hub says the connection ended (disconnected on the account page, or after a password reset), Kelvin forgets the token and says so.
- **Disconnect:** Kelvin forgets the token at once, then ends the connection on the hub (`DELETE /api/token`, confirmed only by a 204). A sync in progress stops before its next request. What was shared stays on the hub, and the account page deletes it.
- **Background sync:** every two minutes, and right after connecting or **Sync now**, Kelvin sends what changed with `POST /api/sync`, projects first, in batches of at most 500 items or 16 MB.
  - **What goes:** what `collect` returns: each chat with its marks and rules, and shared projects with theirs. Plus the finished runs in the person's data folder (those with a `meta.json`) in a shared project or in none.
  - **Whose runs:** only runs this person made (`meta.json` notes `user`), so a data folder shared with someone else never sends their runs.
  - **Runs that stay:** a run's `meta.json` says when an incognito chat started it (`incognito`), or a chat that keeps an unshared project's marks or limits (`unshared`, whatever project the run is filed under). Neither is ever sent.
  - **Older runs:** some runs are held back: those made before Kelvin noted who made them, those made without accounts, and those of queue items from before (`legacy`). They go only when the person ticks **Also share older runs** (`hub_old_runs`, off by default). The status says how many stay back.
  - **Files:** then it uploads each new or changed run file (`meta.json`, `data.csv`, `plan.yaml`, plots, reports; up to 200 MB each), and tells the hub what is gone since (`removed`). A file the hub refuses (400, 413) goes again only when it changes. When nothing changed, it sends nothing.
  - **Retries:** a file the hub fails on (429 or 5xx, such as Cloudflare's 524 on a slow upload), or whose connection breaks while it goes (a timeout), waits while the others go on. It waits 5 minutes, then twice as long each time, up to 4 hours (`retry` in `hub-sync.json`). Three such failures in a row end that sync.
- **Marks on chats** (`web/store.py`):
  - **Leaving a project:** a chat moved out of an unpublished, confidential or model-limited project, or left when the project is deleted, keeps that mark and the project's limits as they are then. Changing a project's limits locks no chat. A chat keeps limits only when it runs under them, leaves the project or is left when it is deleted.
  - **Notes:** a chat in unshared work keeps no notes for every conversation.
  - **Runs:** its runs are filed in its own project, without another project's mounted sample.
- **What was sent** is kept in `hub-sync.json` in the person's folder (`~/Kelvin/users/<name>/`): ids and fingerprints, never content, and a digest of the token it belongs to. A new connection sends everything once more.
- **Safety:**
  - **Snapshot:** a sync works from a snapshot of the person's settings, chats and data folder. It stops when someone else signs in, or when the person disconnects or connects again, so work never goes out under another connection. A 401 that arrives after such a change never wipes the new token.
  - **Redirects:** Kelvin never follows a redirect from the hub, so the token can't be sent on to another address.
  - **Unreachable or new terms:** with the hub unreachable, Kelvin tries again at the next sync. With new terms, it waits until the person agrees to them on the hub.

| Portal call | What it does |
|---|---|
| `GET /api/hub` | `{"connected", "url", "account", "problem", "sync", "synced_at", "syncing"}`, never the token |
| `POST /api/hub/connect` | `{"url"}`: open the hub's connect page in the browser |
| `GET /hub/callback` | Where the hub sends the browser back (this computer only) |
| `POST /api/hub/sync` | Sync now |
| `POST /api/hub/disconnect` | Disconnect, here and on the hub |

## Running it

From a checkout with the `web` (or `hub`) extra installed:

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

While the hub runs, it re-reads `hub.json` when the file changes. If the file can't be read at that moment (half saved, or between an editor's delete and rename), it keeps the settings it has and tries again later. It never writes defaults over it. `KELVIN_HUB_HOME` can stand in for `--home`.

The hub's folder:

| Path | What it is |
|---|---|
| `hub.sqlite3` | Accounts, labs, connected Kelvins and what they share (SQLite, WAL). An older database is brought up to date when the hub starts (schema 3) |
| `hub.json` | Settings: `base_url`, `consent_version`, `require_confirmed_email`, `max_file_mb` (200), `quota_gb_per_user` (5), `backups_kept` (30), `client_ip_header`, `mail`, and the operator key's hash. No secrets |
| `operator-key.txt` | The operator key, written once. Move it to a password manager, then delete the file |
| `files/<user_id>/<run_id>/` | Run files |
| `backups/hub-YYYY-MM-DD.sqlite3` | One copy of the database a day, made while the hub runs (SQLite's online backup) |
| `outbox/` | Mail the hub would have sent, while no SMTP server is set |
| `terms.html`, `privacy.html` | Optional: your own terms and privacy notice, replacing the built-in drafts |
| `hub.log` | Sign-ins, connections, syncs and errors by account number; never addresses, passwords or tokens |

**Backups:** the daily copy covers the database only. Back up `files/` with the rest of the machine.

## The owner console

`/admin` asks for the operator key. It is the Kelvin team's, not an account or a role. No user sees any of this.
- **The hub:** people, confirmed, disabled, active in 30 days, computers, sign-ins, the database's size and the last backup. **Back up the database now** makes a backup at once.
- **People:** everyone, newest first, 200 to a page (**Older** and **Newer**), and searchable by name or email. Each row shows whether the email is confirmed, how many computers the person is signed in on, the Kelvin version they last used, when they joined and when they were last seen. **Disable** stops an account from signing in and ends its sign-ins on every computer; **Enable** undoes it.
- **Download everyone (CSV):** `kelvin-people.csv`, one row per person: id, name, email, ORCID iD, email confirmed, joined, last seen, disabled, computers, Kelvin version, terms agreed. It never includes a password or its hash.
- **A person's page** (`/admin/users/<id>`, from their name): the account, the computers they sign in to Kelvin on (name, system, Kelvin version, first sign-in, last used), and the hub's record of the account (sign-ins, changes, resets, with IP addresses), newest first.
- **Recent activity:** the last 60 entries of the hub's record, each linked to its account.

`GET /api/admin/stats` with an `X-Operator-Key` header gives the counts as JSON.

## Putting it online

1. Run it on an always-on machine, still on `127.0.0.1`.
2. Reach it through [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (free https, no open ports) or a university hostname.
3. Start it with `--base-url https://hub.example.org`, which is saved in `hub.json`. Links in mails use this address, and the hub answers only to it and to `127.0.0.1`/`localhost`. Behind Cloudflare, also set `"client_ip_header": "CF-Connecting-IP"`, so sign-in limits and the record see real addresses. The hub believes that header only from a proxy on its own computer (`cloudflared`), and takes its last value.
4. Set `mail` in `hub.json` (`smtp_host`, `smtp_port`, `starttls`, `username`, `from`) and the password in the `KELVIN_HUB_SMTP_PASSWORD` environment variable. Then turn on `require_confirmed_email`.
5. Replace the draft terms and privacy notice (`terms.html`, `privacy.html`), and raise `consent_version` whenever they change.

Kelvin accepts a hub address on the internet only with https.

## Safety

- **Pages:** no JavaScript, no framing, and a Content-Security-Policy on every response. Forms carry an anti-forgery value, and posts from other sites are refused.
- **Files:** run files are always downloaded, never shown in the browser.
- **Rate limits:** kept in memory, so a restart resets them. An IPv6 address counts by its /64. These are all of them:

  | What | Limit |
  |---|---|
  | Signing in, and the password on the connect page | 10 failures in 15 minutes, per email and per address |
  | New accounts | 20 an hour per address |
  | Reset mails | 20 an hour per address |
  | Token trades | 30 failures in 15 minutes per address |
  | Operator key | 5 failures in 15 minutes per address |
  | Exports | one per person per 10 minutes |
  | Syncs and uploads | 3 of one person's at once; more get a 429 at once. Each waits at most 10 seconds for that person's other sync or upload, then gets a 429 |

  Account-page forms have none. One person's flood of syncs or uploads never takes the worker threads from anyone else, and all syncs and uploads together hold at most 16 of the hub's 40 worker threads (the rest wait their turn without one), so pages and sign-ins always have threads left. A sync's JSON is read in a worker thread, never on the event loop.
- **Request bodies** are capped as they stream in: forms at 64 kB, token trades at 16 kB, one sync at 64 MB.
- **Uploads:**
  - names are checked part by part (no `..`, and no device names, even with a space as in `NUL .txt`);
  - each file needs a `Content-Length` and may be at most `max_file_mb`;
  - its bytes are held against the person's share (`quota_gb_per_user`, which chats count toward too) before it streams in, so parallel uploads can't overrun the share. The share is summed from each chat's stored size, never by reading the chats;
  - at most 2 uploads at a time per person.
- **Times** Kelvin sends must be numbers from the year 2000 to a day from now; anything else is stored as the moment it arrived.

## API

| Call | What it does |
|---|---|
| `GET /api/health` | `{"ok": true, "hub": version}` |
| `POST /api/signin` | Kelvin's sign-in: `{"email", "password", "installation"}` → `{"access_token", "user", "needs_consent", "account_url"}` |
| `POST /api/register` | Kelvin's Create account: `{"name", "email", "password", "consent": true, "orcid"?, "installation"}` → as `signin` |
| `POST /api/reset` | `{"email"}`: mail a reset link → always `{"ok": true}` |
| `POST /api/password` | `{"old", "new"}`: change the password |
| `POST /api/signout` | End this Kelvin's or phone's sign-in: its token stops working (204) |
| `WS /api/relay/kelvin` | A signed-in Kelvin's connection for its person's phones (see Phones through the hub) |
| `GET /api/relay/kelvins` | The account's Kelvins: `{"kelvins": [{"id", "name", "platform", "version", "online", "direct", "last_seen"}], "user"}` |
| `* /api/relay/kelvins/{id}/api/...` | A phone's call to one of them, passed through. 503 with `offline: true` when it isn't online |
| `POST /api/token` | `{"grant_type": "authorization_code", "code", "code_verifier", "redirect_uri"}` → `{"access_token", "user", "lab"}` |
| `DELETE /api/token` | Disconnect this Kelvin |
| `GET /api/me` | The person, their labs, the lab this Kelvin shares into, `needs_consent` |
| `POST /api/sync` | `{"projects": [...], "chats": [...], "runs": [...], "removed": {"projects", "chats", "runs"}}`, at most 500 of each → counts, plus what was refused and why. 413 over 64 MB |
| `PUT /api/sync/runs/{run}/files/{name}` | One run file, as the raw body with a `Content-Length` (send the run first). Errors: 411 without a length, 413 over the size or the person's share, 429 with 2 uploads already running |

Every call but `health`, `token`, `signin`, `register` and `reset` needs `Authorization: Bearer <token>`. Items are keyed by the Kelvin's own ids:
- projects: `id`, `name`, and `shared`, `unpublished`, `confidential`, `rules` as in Kelvin;
- chats: `id`, `project`, `title`, `messages` (up to 5 MB), `updated`, and `incognito`, `shared`, `unpublished`, `confidential`, `rules`;
- runs: `id`, `project`, `device`, `meta` (up to 256 kB), `created`, and `incognito`, `unpublished`, `confidential` (also read from `meta`).

## Known limits

- **New projects:** a new project's name and chats go up within two minutes, before it can be marked unpublished. Marking it later deletes them from the hub.
- **Backups:** they keep deleted work for up to 30 days.
- **Phones:** a paired phone with control can set a project back to Shared.
- **Through the hub:** the hub can read what it passes between a phone and a Kelvin; unshared work never goes that way, but the rest does, in passing.
- **Registration:** it says when an email already has an account.
- **Quoting:** Kelvin's tools that read runs can quote an unshared run into a shared chat. The chat then carries the quote to the hub.
- **Each sync** looks through everything for what changed.

## Not built yet

- Seeing labmates' shared work from inside Kelvin. For now it is on the hub's account page.
- Removing one file of a shared run from the hub. A run removed in Kelvin goes with all its files.
- Signing in with ORCID or Google (the `external_identity` column is there for it).
- Changing one's email address.
- Where the hub runs for real, and its mail server.
- End-to-end encryption between a phone and a Kelvin through the hub, and push notifications (APNs) through it.
