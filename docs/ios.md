# Kelvin for iPhone

A native iPhone and iPad app that is a remote for Kelvin on a lab computer: the lab at a glance, Kelvin's requests to approve or decline, the run in progress with a live plot and **Stop**, chats, the lab's team chat, and past runs with their reports and Analysis. It talks to Kelvin directly, on the lab network or through Tailscale, or, signed in with a Kelvin account, through the Kelvin hub from any network (the hub passes requests through and keeps none).

How people use it is in [portal.md](portal.md#phone-and-remote-access). This page is for building it.

**Try a demo lab** on the welcome screen adds a made-up lab that answers inside the app: a run in progress, a request, an alarm, past runs, a chat and a team chat. Its Analysis works out some steps and fits a line itself (a field sweep's Hall voltage, antisymmetrized, gives 0.4 µV/T); the other steps and models say they need Kelvin. It is for trying the app before pairing, and for App Review, which has no Kelvin computer. CI screenshots it on every change (the `ios-screenshots` artifact of the iOS app workflow).

## What's where

| Path | What |
|---|---|
| `ios/project.yml` | The Xcode project, as an [XcodeGen](https://github.com/yonaskolb/XcodeGen) spec. `Kelvin.xcodeproj` and `Support/Info.plist` are generated from it and not committed. |
| `ios/KelvinKit/` | A Swift package with no UI and only Foundation: the phone API client, the models, the chat stream and transcript, plot data, the team chat thread (`Team.swift`), Analysis specs and results and fitted values with their errors (`Analysis.swift`), the AI company behind a chat's model (`AIService.swift`). Its tests run on Linux too. |
| `ios/Kelvin/` | The SwiftUI app: `AppModel.swift` (the computers, one `LabSession` per computer, `ChatModel`), `Platform.swift` (URLSession, Keychain, notifications), `Dictation.swift` (voice input), `Demo.swift` (the demo lab), `TeamView.swift` and `AnalysisView.swift` (with their models) and the other screens. |
| `ios/KelvinTests/` | App tests: the Keychain, falling back to the Tailscale address, a chat, a message that couldn't be sent or that Kelvin refused, an answer cut short, Stop before Kelvin has the message, a chat that answers without the phone, signing in to the account again, never a public address directly, deleting the account, a run's file name, notifications, the team chat and its unread count, Analysis on the demo lab, asking before a chat's first message goes to an AI company. `ios/KelvinUITests/`: the keyboard, the message bar at the largest text size, the privacy notice on the welcome screen, and a walkthrough of every screen on the demo lab. |
| `.github/workflows/ios.yml` | CI: KelvinKit's tests on Linux; when they pass, the app's build and tests on the iOS Simulator (macOS runner), screenshots of the demo lab (`ios-screenshots`), and the App Store's: a 6.9-inch iPhone and a 13-inch iPad, light (`app-store-screenshots`). |
| `.github/workflows/ios-testflight.yml` | On a tag `ios-v*`: builds with the newest Xcode, signs with an App Store Connect API key and uploads to TestFlight. |

The app needs iOS 16. It uses only Apple's frameworks (SwiftUI, Charts, Speech for voice input, QuickLook for reports).

Voice input uses on-device recognition only (`requiresOnDeviceRecognition`). Where the iPhone can't recognize the language on the phone itself, the mic button says so, and the app doesn't fall back to Apple's servers. Voice only fills the message box; it never sends or approves anything.

## The API it uses

The phone listener (`src/labharness/web/remote.py`) and the routes a phone may call with its own token (given through the hub by `POST /api/phone/direct`; a computer paired by an app before 0.1.8 keeps its pairing's):

- Every request carries the token in `X-Kelvin-Token`.
- `GET /api/me` → role, phone name, lab, and for the app: `computer`, `version`, `api` (1), `addresses` (where the phone listener is: the lab network and Tailscale) and `routes` (each address with its kind: `tailscale`, `network` or `public`). The app keeps them, tries the address that worked last first, and calls a connection encrypted only when Kelvin says the address is Tailscale's: some Wi-Fi networks (Xfinity's) hand out addresses in Tailscale's range 100.64.0.0/10 too.
- Watch and control: `GET /api/state`, `/api/live?since=n`, `/api/runs`, `/api/runs/{id}`, `/api/runs/{id}/files/{name}`; `POST /api/approvals/{id}` (control only for approving), `/api/alarms/{id}/ack`, `/api/runs/stop`, `/api/stop {chat_id}` (a chat's Stop: that chat and its lab work, nothing else), `/api/me/forget`, `/api/hands {on}` (the hands-on-the-sample lock: any phone can lock, control only can unlock). `/api/state` carries `mounted` (the sample, with `protection_lines`) and `hands_on` (who locked it and since when) from Kelvin 0.3.17; older Kelvins leave them out. Each of its `alarms` also carries its history: `count`, `last`, `cleared_text`, `acknowledged_by`, `acknowledged_at` and `latched` (a guard's trip or a wait that gave up, on until Got it); `alarms` lists every alarm nobody has said Got it to, cleared or not, besides the latest 20. `POST /api/runs/stop {}` with no run in progress while the queue runs stops the queue. `/api/runs` gives each run's `started_at` (seconds since 1970) from Kelvin 0.3.34, so a phone in another time zone shows the right time; before, the app reads the lab computer's clock time from the run id.
- Control: `GET /api/bootstrap` (models), `/api/chats`, `/api/chats/{id}`; `POST /api/chats {model, incognito}`, `/api/chats/{id}/turn {text, model}` (the answer streams back as one JSON event per line, each ended by `\n` alone: U+2028, U+2029 and U+0085 in its text come escaped. A stream that ends without `turn_end` was cut: the app says it lost the connection, shows the steps it didn't see end as not known, and loads the chat again; a chat Kelvin answers without the phone, by `/api/state`'s `answering`, shows as working, with **Stop**).
- The team chat (Kelvin 0.3.32 and later): `GET /api/team?after=<id>` (or `?before=<id>`, scrolling back) → `{messages, last}`, each message `{id, at, name, account, via, text}`, oldest first, 200 at most; any phone reads it. `POST /api/team {name, text}` (control) → `{message, last}`. `/api/state` carries `team`, the newest message's id: the tab's badge counts from the last one the phone showed (kept per computer in UserDefaults), and what was there at its first look is not news. The name a person writes under is the phone's own (`kelvin.teamName`).
- Analysis (Kelvin 0.3.32 and later, control): `GET /api/analysis/columns?run=<id>` → the run's `axes`, `readings`, `traces`, `kept` fits and `models`; `POST /api/analysis {spec}` → `{curves, axes, notes}` (fits may take 45 s: the app waits 90); `POST /api/analysis/keep {spec, curve, replace}` → `{said}`, or 409 `{exists}` (the app asks before replacing); `POST /api/analysis/csv {spec}` → the CSV, named by `Content-Disposition`. The spec always names its runs: through the hub, Kelvin refuses "the last run" and unshared runs. Newer Kelvins add fields an older app leaves alone: the columns' `others` (`i`, `t`, `repeat`) and `hold` (the models that need held values, with their names), the result's `group` (what the curves are drawn per) and `params` (the fit's parameters, for `start` and `fix` in the spec's fit), and scale's `unit`. A spec without `group` draws a map a curve per value of its outer axis; `group: ""` is one curve.

Through the Kelvin account (Kelvin 0.3.19 and later; see [hub.md](hub.md#phones-through-the-hub)), at the one Kelvin hub built into the app (`Account.defaultHub`; **Use a test hub** on the sign-in screen names another):

- `POST <hub>/api/signin {email, password, installation: {id: "ios-<uuid>", name, platform: "ios", version}}` → the phone's token on the hub (in the Keychain, under `Account.tokenID`); `GET <hub>/api/relay/kelvins` → the account's Kelvins; `POST <hub>/api/signout` ends it.
- The phone routes above, through `<hub>/api/relay/kelvins/<id>/api/...`, with `Authorization: Bearer <token>` (`KelvinClient(bearer: true)`, whose `base` is that path). A 503 with `offline: true` means that Kelvin isn't online.
- From app 0.1.8 there is no pairing: the phone reaches Kelvin only through the Kelvin account. A computer paired by an older app stays until removed; when the account reaches it too, it is one row that goes through the hub with the account's token away from the lab.
- `POST /api/phone/direct` (through the hub only) → `{available, token, addresses, routes}`: the phone's own token for reaching that Kelvin directly, tried first (2.5 s) before the hub, at its lab-network and Tailscale addresses only: never a `public` one, which would carry it across the internet in plain http. `DELETE` forgets it: when the phone signs out, and before it deletes the account.

When a route the app depends on changes, raise `API` in remote.py and handle both in the app.

## Build it on a Mac

```bash
brew install xcodegen
```

```bash
cd ios && xcodegen generate && open Kelvin.xcodeproj
```

Choose the **Kelvin** scheme and a simulator, then Run. Sign in there with an account on a hub of your own (**Use a test hub** on the sign-in screen; see [hub.md](hub.md)), or try the demo lab.

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

## App Store

The App Store takes the same build as TestFlight: a tag `ios-v*` uploads it, and App Store Connect sends it to App Review with a store page. Everything below is done in App Store Connect, signed in with your Apple ID; the CI's API key only uploads builds.

### Once: before the first submission

1. **Finish the hub's terms and privacy notice.** Both still open with "Draft". The app links the privacy notice (the welcome screen, the sign-in sheet, Computers → Kelvin account) and so does the store page, so it must be final, and the hub must be up while Apple reviews.
2. **A review account:** a Kelvin account on the hub, for Apple. The reviewer may try **Delete Kelvin account…** on it; make another if so.
3. **App Information:** subtitle and category (below); content rights: no third-party content; the age rating questions: messaging yes (the team chat), and count what an AI model could write in a chat when answering the content questions. Apple works out the rating.
4. **App Privacy:** the privacy policy URL is the hub's `/privacy` (`https://kelvin.tail269ee5.ts.net/privacy` while the hub runs on the workstation). The data, from the hub's privacy notice, all linked to the person and none used for tracking:
   - Contact info: name and email address (App Functionality).
   - Identifiers: user ID, the account; device ID, the phone's installation id (App Functionality).
   - User content: other user content, the chats and run records Kelvin shares with the hub unless switched off (App Functionality; Analytics too while the Kelvin team reads shared work to improve Kelvin).
5. **Pricing and Availability:** free. Leave out China mainland, which asks for a government (ICP) filing number.
6. **EU trader status** (Business in App Store Connect), without which the app isn't offered in the EU. A trader's address, phone and email show on EU store pages.

### Each version

1. Push a tag `ios-v<version>`, such as `ios-v1.0.0`. The build appears under TestFlight 10 to 30 minutes later.
2. **iOS App → + Version** with the same version number (the first version is there already).
3. **Screenshots:** the `app-store-screenshots` artifact of the iOS app workflow on that commit. Its iPhone folder goes under 6.9-inch Display, its iPad folder under 13-inch Display.
4. The store text (below) and, from the second version on, What's New.
5. **Build:** the one from TestFlight.
6. **App Review Information:** sign-in required, with the review account, and the note below. A short screen recording of the app with a real lab helps, since the reviewer has none.
7. **Version Release:** Manually release this version. Then **Add for Review** and **Submit**. Most reviews take a day or two; once approved, **Release This Version**.

### The store page

- **Name:** the one the app was made with in App Store Connect. The home screen says Kelvin either way.
- **Subtitle:** Watch and steer your lab
- **Category:** Productivity; second, Utilities.
- **Promotional text:** Your lab at a glance: approve Kelvin's requests, stop a run and chat with Kelvin, from anywhere.
- **Keywords:** `lab,experiment,physics,chemistry,measurement,instrument,research,scientist,AI,assistant,remote`
- **Support URL:** https://github.com/Houchen181/kelvin/issues
- **Description:**

  ```text
  Kelvin is an AI lab assistant for experimental scientists. It runs on the computer that drives your instruments, plans and runs measurements with you, and waits for a person before it drives the sample.

  This app is its remote. Sign in with your Kelvin account and reach every Kelvin you use, on the lab network or from anywhere:

  • The lab at a glance: instruments, readings and alarms
  • Kelvin's requests, to approve or decline
  • The run in progress, with a live plot and Stop
  • Chats with Kelvin, typed or spoken (speech turns into text on the iPhone itself)
  • The lab's team chat
  • Past runs, their reports, and analysis with fits

  You need Kelvin on a lab computer (Windows) and a Kelvin account. No lab yet? Try the demo lab on the first screen.

  Kelvin's answers come from the AI model chosen on the lab computer. Before a chat first goes to an AI company, the app asks.
  ```

- **App Review note:**

  ```text
  No lab computer needed: tap "Try a demo lab" on the first screen. It answers inside the app and shows every screen: the lab, a request to approve, a run with Stop, a chat, the team chat, past runs and Analysis.

  The app is a remote for Kelvin, a desktop app on the computer that controls a lab's instruments. Signed in with a Kelvin account (the review account above), the phone reaches that person's Kelvin computers; this account has none, so its list stays empty.

  Plain http goes only to the person's own lab computer, on the local network or inside Tailscale (which encrypts it), never to a public server. Chats are answered by the AI model chosen on the lab computer; the app names the AI company and asks before a chat's first message goes there. Deleting the account: Computers → Kelvin account → Delete Kelvin account.
  ```

## Security notes

- The token is in the Keychain (this device only, after first unlock); the list of computers in UserDefaults holds nothing secret.
- App Transport Security: from iOS 17 the app needs `NSAllowsLocalNetworking` to reach a lab IP address, and exceptions for Tailscale's `100.64.0.0/10` and `*.ts.net`. Any other plain-http address is refused by iOS.
- Removing a computer in the app also makes Kelvin there forget the phone's token, when it answers.
- **Delete Kelvin account…** (Computers → Kelvin account) deletes the account on the hub with its password (`POST /api/account/delete`), then signs the phone out, as App Review asks of an app that offers making an account. Each Kelvin forgets the phone's direct token first, while the hub still takes the phone there: once the account is gone, nothing could tell it, and it would go on counting the phone as one to ask from away. Each Kelvin computer keeps the person's chats and measurements until the account is removed there too.
- **Privacy notice:** the welcome screen, the sign-in sheet and Computers → Kelvin account link the hub's `/privacy` (the last two its `/terms` too), as App Review asks of every app (guideline 5.1.1(i)).
- **AI companies:** before a chat's first message goes to an AI company, the app names the company, the model and the computer, and sends only on **Allow** (guideline 5.1.2(i)); **Not now** leaves the message in the box. Each company is allowed once on the phone; Computers → **Ask again before sending** takes it back. A model server of the person's own (`local`) and the demo lab never ask. `AIService` in KelvinKit reads the company from the model's key (`hub-anthropic:…` is Anthropic, through the Kelvin hub); a kind of model connection it doesn't know asks under the name Kelvin's model list gives it. When Kelvin gets a new kind, add its company there.

## Next: push notifications

Once the Kelvin hub runs on a permanent server, it can send push notifications through Apple's push service. Until then, alerts while the app is closed come from Kelvin's ntfy webhook (Settings → Notifications).
