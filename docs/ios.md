# Kelvin for iPhone

A native iPhone and iPad app that is a remote for Kelvin on a lab computer: the lab at a glance, Kelvin's requests to approve or decline, the run in progress with a live plot and **Stop**, chats, the lab's team chat, and past runs with their reports and Analysis. It talks to Kelvin directly, on the lab network or through Tailscale, or, signed in with a Kelvin account, through the Kelvin hub from any network (the hub passes requests through and keeps none).

How people use it is in [portal.md](portal.md#phone-and-remote-access). This page is for building it.

**Try a demo lab** on the welcome screen adds a made-up lab that answers inside the app: a run in progress, a request, an alarm, past runs, a chat and a team chat. Its Analysis works out some steps and fits a line itself (a field sweep's Hall voltage, antisymmetrized, gives 0.4 µV/T); the other steps and models say they need Kelvin. It is for trying the app before pairing, and for App Review, which has no Kelvin computer. CI screenshots it on every change (the `ios-screenshots` artifact of the iOS app workflow).

## What's where

| Path | What |
|---|---|
| `ios/project.yml` | The Xcode project, as an [XcodeGen](https://github.com/yonaskolb/XcodeGen) spec. `Kelvin.xcodeproj` and `Support/Info.plist` are generated from it and not committed. |
| `ios/KelvinKit/` | A Swift package with no UI and only Foundation: pairing links, the phone API client, the models, the chat stream and transcript, plot data, the team chat thread (`Team.swift`), Analysis specs and results and fitted values with their errors (`Analysis.swift`). Its tests run on Linux too. |
| `ios/Kelvin/` | The SwiftUI app: `AppModel.swift` (paired computers, one `LabSession` per computer, `ChatModel`), `Platform.swift` (URLSession, Keychain, notifications), `Dictation.swift` (voice input), `Demo.swift` (the demo lab), `TeamView.swift` and `AnalysisView.swift` (with their models) and the other screens. |
| `ios/KelvinTests/` | App tests: pairing, the Keychain, falling back to the Tailscale address, a chat, a message that couldn't be sent, signing in to the account again, notifications, the team chat and its unread count, Analysis on the demo lab. `ios/KelvinUITests/`: the keyboard, and a walkthrough of every screen on the demo lab. |
| `.github/workflows/ios.yml` | CI: KelvinKit's tests on Linux; when they pass, the app's build and tests on the iOS Simulator (macOS runner). |
| `.github/workflows/ios-testflight.yml` | On a tag `ios-v*`: builds with the newest Xcode, signs with an App Store Connect API key and uploads to TestFlight. |

The app needs iOS 16. It uses only Apple's frameworks (SwiftUI, Charts, AVFoundation for the QR scanner, Speech for voice input, QuickLook for reports).

Voice input uses on-device recognition only (`requiresOnDeviceRecognition`). Where the iPhone can't recognize the language on the phone itself, the mic button says so, and the app doesn't fall back to Apple's servers. Voice only fills the message box; it never sends or approves anything.

## The API it uses

The phone listener (`src/labharness/web/remote.py`) and the routes a paired device may call:

- `POST /api/pair {code}` → `{token, name, role}`. Every later request carries `X-Kelvin-Token`.
- `GET /api/me` → role, phone name, lab, and for the app: `computer`, `version`, `api` (1), `addresses` (where the phone listener is: the lab network and Tailscale) and `routes` (each address with its kind: `tailscale`, `network` or `public`). The app keeps them, tries the address that worked last first, and calls a connection encrypted only when Kelvin says the address is Tailscale's: some Wi-Fi networks (Xfinity's) hand out addresses in Tailscale's range 100.64.0.0/10 too.
- The pairing QR code holds `http://<address>:<port>/pair#<code>&also=<host:port>,…`: the first address, then the others that fit. The app tries each in turn; only a computer that answers sees the code.
- Watch and control: `GET /api/state`, `/api/live?since=n`, `/api/runs`, `/api/runs/{id}`, `/api/runs/{id}/files/{name}`; `POST /api/approvals/{id}` (control only for approving), `/api/alarms/{id}/ack`, `/api/runs/stop`, `/api/stop {chat_id}` (a chat's Stop: that chat and its lab work, nothing else), `/api/me/forget`, `/api/hands {on}` (the hands-on-the-sample lock: any phone can lock, control only can unlock). `/api/state` carries `mounted` (the sample, with `protection_lines`) and `hands_on` (who locked it and since when) from Kelvin 0.3.17; older Kelvins leave them out. Each of its `alarms` also carries its history: `count`, `last`, `cleared_text`, `acknowledged_by`, `acknowledged_at` and `latched` (a guard's trip or a wait that gave up, on until Got it); `alarms` lists every alarm nobody has said Got it to, cleared or not, besides the latest 20. `POST /api/runs/stop {}` with no run in progress while the queue runs stops the queue. `/api/runs` gives each run's `started_at` (seconds since 1970) from Kelvin 0.3.34, so a phone in another time zone shows the right time; before, the app reads the lab computer's clock time from the run id.
- Control: `GET /api/bootstrap` (models), `/api/chats`, `/api/chats/{id}`; `POST /api/chats {model, incognito}`, `/api/chats/{id}/turn {text, model}` (the answer streams back as one JSON event per line).
- The team chat (Kelvin 0.3.32 and later): `GET /api/team?after=<id>` (or `?before=<id>`, scrolling back) → `{messages, last}`, each message `{id, at, name, account, via, text}`, oldest first, 200 at most; any phone reads it. `POST /api/team {name, text}` (control) → `{message, last}`. `/api/state` carries `team`, the newest message's id: the tab's badge counts from the last one the phone showed (kept per computer in UserDefaults), and what was there at its first look is not news. The name a person writes under is the phone's own (`kelvin.teamName`).
- Analysis (Kelvin 0.3.32 and later, control): `GET /api/analysis/columns?run=<id>` → the run's `axes`, `readings`, `traces`, `kept` fits and `models`; `POST /api/analysis {spec}` → `{curves, axes, notes}` (fits may take 45 s: the app waits 90); `POST /api/analysis/keep {spec, curve, replace}` → `{said}`, or 409 `{exists}` (the app asks before replacing); `POST /api/analysis/csv {spec}` → the CSV, named by `Content-Disposition`. The spec always names its runs: through the hub, Kelvin refuses "the last run" and unshared runs. Newer Kelvins add fields an older app leaves alone: the columns' `others` (`i`, `t`, `repeat`) and `hold` (the models that need held values, with their names), the result's `group` (what the curves are drawn per) and `params` (the fit's parameters, for `start` and `fix` in the spec's fit), and scale's `unit`. A spec without `group` draws a map a curve per value of its outer axis; `group: ""` is one curve.

Through the Kelvin account (Kelvin 0.3.19 and later; see [hub.md](hub.md#phones-through-the-hub)), at the one Kelvin hub built into the app (`Account.defaultHub`; **Use a test hub** on the sign-in screen names another):

- `POST <hub>/api/signin {email, password, installation: {id: "ios-<uuid>", name, platform: "ios", version}}` → the phone's token on the hub (in the Keychain, under `Account.tokenID`); `GET <hub>/api/relay/kelvins` → the account's Kelvins; `POST <hub>/api/signout` ends it.
- The phone routes above, through `<hub>/api/relay/kelvins/<id>/api/...`, with `Authorization: Bearer <token>` (`KelvinClient(bearer: true)`, whose `base` is that path). A 503 with `offline: true` means that Kelvin isn't online.
- A paired computer the account reaches too is one row, the pairing: away from the lab it goes through the hub with the account's token (app 0.1.8). Pairing itself needs the phone on the computer's network or Tailscale; when it can't reach the computer, the app offers the Kelvin account.
- `POST /api/phone/direct` (through the hub only) → `{available, token, addresses, routes}`: the phone's own token for reaching that Kelvin directly, tried first (2.5 s) before the hub. `DELETE` forgets it.

When a route the app depends on changes, raise `API` in remote.py and handle both in the app.

## Build it on a Mac

```bash
brew install xcodegen
```

```bash
cd ios && xcodegen generate && open Kelvin.xcodeproj
```

Choose the **Kelvin** scheme and a simulator, then Run. The simulator has no camera, so pair by typing the address and code (run Kelvin on the same Mac, switch on phone access, and pair with the address it shows).

To run it on your own iPhone, set your team under Signing & Capabilities (or `DEVELOPMENT_TEAM` in `project.yml`), plug the phone in and Run. A free Apple account works for your own phone for 7 days at a time.

KelvinKit alone, on any machine with Swift:

```bash
swift test --package-path ios/KelvinKit
```

## TestFlight

People install the app through TestFlight: you send a link, they install Apple's TestFlight app and tap it. Up to 10,000 people; each build works for 90 days. It needs an Apple Developer Program membership (99 USD a year). No Mac is needed: `.github/workflows/ios-testflight.yml` builds, signs and uploads on GitHub's Mac when a tag such as `ios-v0.1.0` is pushed.

### Once: the account

1. **Enroll** at [developer.apple.com/programs/enroll](https://developer.apple.com/programs/enroll/) as an individual, with an Apple ID that has two-factor authentication. Use your legal name, and pay the 99 USD. Apple usually confirms within 48 hours. The Apple Developer app on an iPhone enrolls fastest.
2. **Team ID:** [developer.apple.com/account](https://developer.apple.com/account) → Membership details.
3. **Register the app id:** Certificates, Identifiers & Profiles → Identifiers → + → App IDs → App, with the explicit bundle id `io.github.houchen181.kelvin` and the description Kelvin. It needs no capabilities.
4. **The app in App Store Connect:** [appstoreconnect.apple.com](https://appstoreconnect.apple.com) → Apps → + → New App. Choose iOS, a name, English, the bundle id above, the SKU `kelvin-ios` and full access. The name must be unique on the App Store, and "Kelvin" is probably taken, so try something like "Kelvin Lab". The app still shows as Kelvin on the home screen.
5. **The API key for CI:** App Store Connect → Users and Access → Integrations → App Store Connect API → Team Keys → +. Name it Kelvin CI with **Admin** access, so the CI can make the distribution certificate itself. Download the `.p8` file; Apple offers it only once. Note its Key ID and the Issuer ID shown above the list.
6. **Give them to GitHub yourself.** Never paste them into a chat. In a terminal, from the repository folder, run each command; it asks for the value without showing it:

   ```bash
   gh secret set APPLE_TEAM_ID
   ```

   ```bash
   gh secret set ASC_KEY_ID
   ```

   ```bash
   gh secret set ASC_ISSUER_ID
   ```

   ```bash
   gh secret set ASC_KEY_P8 < "C:/path/to/AuthKey_XXXXXXXXXX.p8"
   ```

   Then keep the `.p8` file somewhere safe, or delete it: a new key can be made at any time.

### Each release

1. Push a tag `ios-v<version>` on the commit to ship. The build number is the workflow's run number, so it always goes up.
2. App Store Connect processes the build for 10 to 30 minutes, then it appears under TestFlight.
3. **You and your team** (App Store Connect users, up to 100): add yourselves as internal testers. This needs no review.
4. **Everyone else:** create an external group and switch on its public link. Under Test Information, fill in a short description, a feedback email and, for the reviewer, "No lab computer needed: tap Try a demo lab". The first build of each version goes through Apple's beta review, which usually takes about a day. Then send people the link.
5. If the reviewer asks why the app uses plain http: it connects only to the user's own lab computer, on the local network or inside Tailscale (which encrypts the traffic), never to a public server.

## Security notes

- The token is in the Keychain (this device only, after first unlock); the list of computers in UserDefaults holds nothing secret.
- App Transport Security: from iOS 17 the app needs `NSAllowsLocalNetworking` to reach a lab IP address, and exceptions for Tailscale's `100.64.0.0/10` and `*.ts.net`. Any other plain-http address is refused by iOS.
- Unpairing in the app also unpairs the phone on the Kelvin computer when it answers.
- **Delete Kelvin account…** (Computers → Kelvin account) deletes the account on the hub with its password (`POST /api/account/delete`), then signs the phone out, as App Review asks of an app that offers making an account. Each Kelvin computer keeps the person's chats and measurements until the account is removed there too.

## Next: push notifications

Once the Kelvin hub runs on a permanent server, it can send push notifications through Apple's push service. Until then, alerts while the app is closed come from Kelvin's ntfy webhook (Settings → Notifications).
