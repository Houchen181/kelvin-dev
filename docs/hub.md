# The Kelvin hub

Kelvin keeps accounts per computer ([portal.md](portal.md#signing-in)). The hub is one server for Kelvin users in every lab. People register once, join their labs and connect each Kelvin they use. Their Kelvins then share their work there with their labmates. It is built for a few thousand people (about 5,000): one SQLite file, with run files on disk.

**Status:** the server, its database, a Kelvin's client (`hub/client.py`) and the Kelvin app's side (Settings → **Account** → Kelvin hub, and syncing in the background: `web/hubsync.py`) are built and tested. The terms and privacy notice are drafts.

## What is shared

- **Shared by default:** normal projects and unfiled chats, with their runs and whole conversations.
- **Never sent:**
  - projects marked unpublished or confidential, and projects whose **Who may see this project** limits the models or literature search;
  - incognito chats, and chats that ran under such limits;
  - API keys, and ChatGPT or Claude Code sign-ins.

  Kelvin's client (`collect` in `hub/client.py`) leaves these out, and sends an unshared project only as its id and "not shared", never its name. The hub checks again: it refuses them, and when a project stops being shared it deletes what it had of it.
- **Marking a project:** in Kelvin, a project's **Project settings → On the Kelvin hub** is *Shared* (the default), *Unpublished* or *Confidential*. A chat moved out of an unpublished or confidential project, or left unfiled when that project is deleted, keeps the mark and stays unshared. A run started (or queued) from an incognito chat says so in its `meta.json`, and is never sent. Nor is a run whose project is gone, since Kelvin can't tell whether that project was shared.
- **Who sees it:** its owner, and the other members of the lab it was shared in, while both are members. Leaving a lab takes your work out of its view. A Kelvin connected with "No lab" shares only with its person.

## Accounts and labs

- **Registering:** name, email, an optional ORCID iD (its check digit is checked) and a password of 8 characters or more. Agreeing to the terms and the privacy notice is required, and the hub records which version and when. When the version in `hub.json` changes, everyone is asked to agree again before going on, and syncing pauses until they do.
- **Passwords:** PBKDF2-SHA256, as for Kelvin's local accounts (`accounts.py`). Browser sessions, app tokens and one-use codes are stored only as their SHA-256.
- **Email:** the hub sends a confirmation link, and password reset links. Until an SMTP server is set, these go to `outbox/` in the hub's folder for the operator to pass on. `"require_confirmed_email": true` then stops unconfirmed people from connecting a Kelvin or sharing.
- **Labs have no owner.** Anyone can make a lab. Everyone in it sees its join code (like `K7Q2-9XMB-4TRA`) and can pass it on. A lab nobody is in any more is deleted.
- **Your data:** **Export everything** on the account page downloads a zip of the account, labs, connections, everything shared and the hub's record of the account's activity. **Delete account** (with the password) deletes the account and all it shared. The activity record then keeps only that account number N was deleted, without addresses or details. Database backups keep a deleted account until they age out (30 days by default).

## Connecting a Kelvin

This works like Kelvin's Sign in with ChatGPT, with the hub as the sign-in page:

1. Kelvin opens `<hub>/connect` in the browser, with a PKCE challenge (S256), a state, its own installation id, name, platform and version, and its loopback callback `http://127.0.0.1:<port>/...`. The hub sends the browser back to nothing else.
2. The person signs in, chooses which lab the Kelvin shares into, and presses **Connect**.
3. The hub sends the browser back to the callback with a one-use code (10 minutes). Kelvin trades the code and its PKCE verifier for a token at `POST /api/token`.
4. Kelvin keeps the token in the person's own settings, encrypted with their password. Connecting the same Kelvin again replaces the token. **Disconnect** on the account page, or `DELETE /api/token`, ends it.

`HubClient` in `hub/client.py` does Kelvin's side: `begin`, `finish`, `push`, `upload` and `disconnect`, plus `collect(store)`, which gathers what a `ChatStore` may share.

### In the Kelvin app

Settings → **Account** → Kelvin hub (`web/hubsync.py`, routes in `web/app.py`):

- **Connect:** the person types the hub's address and presses **Connect**. Kelvin opens the hub's `/connect` page in the browser, with its callback `http://127.0.0.1:<port>/hub/callback` and the computer's installation id (`hub_installation` in `~/Kelvin/settings.json`, made once). The callback works only on Kelvin's own computer, never from a paired phone. The hub's address and the token go in the person's `settings.json` (`hub_url`, `hub_token`). The token is encrypted with their password like the API keys, and never reaches the browser or the logs.
- **Status:** who the person is on the hub, which lab this Kelvin shares into, and the last sync (from `GET /api/me`). If the hub says the connection ended (disconnected on the account page), Kelvin forgets the token and says so.
- **Disconnect** also ends the connection on the hub (`DELETE /api/token`). What was shared stays there, and the account page deletes it.
- **Background sync:** every two minutes, and right after connecting or **Sync now**, Kelvin sends what changed with `POST /api/sync`, projects first, in batches of at most 500 items or 16 MB. That is what `collect` returns, plus the finished runs in the person's data folder (those with a `meta.json`) in a shared project or in none. Then it uploads each new or changed run file (`meta.json`, `data.csv`, `plan.yaml`, plots, reports; up to 200 MB each), and tells the hub what is gone since (`removed`). When nothing changed, it sends nothing.
- **What was sent** is kept in `hub-sync.json` in the person's folder (`~/Kelvin/users/<name>/`): ids and fingerprints, never content. Connecting again sends everything once more.
- **Safety:** a sync works from a snapshot of the person's settings, chats and data folder, and stops when someone else signs in, so one person's work never goes out under another's connection. With the hub unreachable, Kelvin tries again at the next sync. With new terms, it waits until the person agrees to them on the hub.

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

`backup` copies the database now, `operator-key` makes a new operator key (the old one stops working) and `stats` prints the counts. `KELVIN_HUB_HOME` can stand in for `--home`.

The hub's folder:

| Path | What it is |
|---|---|
| `hub.sqlite3` | Accounts, labs, connected Kelvins and what they share (SQLite, WAL) |
| `hub.json` | Settings: `base_url`, `consent_version`, `require_confirmed_email`, `max_file_mb` (200), `quota_gb_per_user` (5), `backups_kept` (30), `client_ip_header`, `mail`, and the operator key's hash. No secrets |
| `operator-key.txt` | The operator key, written once. Move it to a password manager, then delete the file |
| `files/<user_id>/<run_id>/` | Run files |
| `backups/hub-YYYY-MM-DD.sqlite3` | One copy of the database a day, made while the hub runs (SQLite's online backup) |
| `outbox/` | Mail the hub would have sent, while no SMTP server is set |
| `terms.html`, `privacy.html` | Optional: your own terms and privacy notice, replacing the built-in drafts |
| `hub.log` | Sign-ins, connections, syncs and errors; never passwords or tokens |

**Backups:** the daily copy covers the database only. Back up `files/` with the rest of the machine.

## The operator page

`/admin` asks for the operator key. It is the Kelvin team's, not an account or a role. The page shows the counts, finds people by name or email, disables and re-enables accounts (a disabled account can't sign in, and its Kelvins are disconnected), shows the recent activity, and backs up the database. `GET /api/admin/stats` with an `X-Operator-Key` header gives the counts as JSON.

## Putting it online

1. Run it on an always-on machine, still on `127.0.0.1`.
2. Reach it through [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (free https, no open ports) or a university hostname.
3. Start it with `--base-url https://hub.example.org`, which is saved in `hub.json`. Links in mails use this address, and the hub answers only to it and to `127.0.0.1`/`localhost`. Behind Cloudflare, also set `"client_ip_header": "CF-Connecting-IP"`, so sign-in limits and the record see real addresses.
4. Set `mail` in `hub.json` (`smtp_host`, `smtp_port`, `starttls`, `username`, `from`) and the password in the `KELVIN_HUB_SMTP_PASSWORD` environment variable. Then turn on `require_confirmed_email`.
5. Replace the draft terms and privacy notice (`terms.html`, `privacy.html`), and raise `consent_version` whenever they change.

Kelvin accepts a hub address on the internet only with https.

## Safety

- **Pages:** no JavaScript, no framing, and a Content-Security-Policy on every response. Forms carry an anti-forgery value, and posts from other sites are refused.
- **Files:** run files are always downloaded, never shown in the browser.
- **Limits:** sign-in is limited per email and per address (10 failures in 15 minutes), and so are new accounts, reset mails, token trades and operator-key tries. The limits are kept in memory.
- **Uploads:** names are checked part by part (no `..`, no device names). Each file may be at most `max_file_mb`, and each person at most `quota_gb_per_user`.

## API

| Call | What it does |
|---|---|
| `GET /api/health` | `{"ok": true, "hub": version}` |
| `POST /api/token` | `{"grant_type": "authorization_code", "code", "code_verifier", "redirect_uri"}` → `{"access_token", "user", "lab"}` |
| `DELETE /api/token` | Disconnect this Kelvin |
| `GET /api/me` | The person, their labs, the lab this Kelvin shares into, `needs_consent` |
| `POST /api/sync` | `{"projects": [...], "chats": [...], "runs": [...], "removed": {"projects", "chats", "runs"}}`, at most 500 of each → counts, plus what was refused and why |
| `PUT /api/sync/runs/{run}/files/{name}` | One run file, as the raw body (send the run first) |

Every call but `health` and `token` needs `Authorization: Bearer <token>`. Items are keyed by the Kelvin's own ids:
- projects: `id`, `name`, and `shared`, `unpublished`, `confidential`, `rules` as in Kelvin;
- chats: `id`, `project`, `title`, `messages` (up to 5 MB), `updated`, `incognito`, `rules`;
- runs: `id`, `project`, `device`, `meta` (up to 256 kB), `created`.

## Not built yet

- Seeing labmates' shared work from inside Kelvin. For now it is on the hub's account page.
- Removing one file of a shared run from the hub. A run removed in Kelvin goes with all its files.
- Signing in with ORCID or Google (the `external_identity` column is there for it).
- Changing one's email address.
- Where the hub runs for real, and its mail server.
