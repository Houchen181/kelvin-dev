# Kelvin for iPhone

A native iPhone and iPad app that is a remote for Kelvin on a lab computer: the lab at a glance, Kelvin's requests to approve or decline, the run in progress with a live plot and **Stop**, chats, and past runs with their reports. It talks to Kelvin directly, on the lab network or through Tailscale; nothing goes through anyone else's server.

How people use it is in [portal.md](portal.md#phone-and-remote-access). This page is for building it.

**Try a demo lab** on the welcome screen adds a made-up lab that answers inside the app: a run in progress, a request, an alarm, past runs and a chat. It is for trying the app before pairing, and for App Review, which has no Kelvin computer. CI screenshots it on every change (the `ios-screenshots` artifact of the iOS app workflow).

## What's where

| Path | What |
|---|---|
| `ios/project.yml` | The Xcode project, as an [XcodeGen](https://github.com/yonaskolb/XcodeGen) spec. `Kelvin.xcodeproj` and `Support/Info.plist` are generated from it and not committed. |
| `ios/KelvinKit/` | A Swift package with no UI and only Foundation: pairing links, the phone API client, the models, the chat stream and transcript, plot data. Its tests run on Linux too. |
| `ios/Kelvin/` | The SwiftUI app: `AppModel.swift` (paired computers, one `LabSession` per computer, `ChatModel`), `Platform.swift` (URLSession, Keychain, notifications), `Dictation.swift` (voice input), `Demo.swift` (the demo lab) and the screens. |
| `ios/KelvinTests/` | App tests: pairing, the Keychain, falling back to the Tailscale address, a chat, notifications. |
| `.github/workflows/ios.yml` | CI: KelvinKit's tests on Linux; when they pass, the app's build and tests on the iOS Simulator (macOS runner). |
| `.github/workflows/ios-testflight.yml` | On a tag `ios-v*`: builds with the newest Xcode, signs with an App Store Connect API key and uploads to TestFlight. |

The app needs iOS 16. It uses only Apple's frameworks (SwiftUI, Charts, AVFoundation for the QR scanner, Speech for voice input, QuickLook for reports).

Voice input uses on-device recognition only (`requiresOnDeviceRecognition`). Where the iPhone can't recognize the language on the phone itself, the mic button says so, and the app doesn't fall back to Apple's servers. Voice only fills the message box; it never sends or approves anything.

## The API it uses

The phone listener (`src/labharness/web/remote.py`) and the routes a paired device may call:

- `POST /api/pair {code}` → `{token, name, role}`. Every later request carries `X-Kelvin-Token`.
- `GET /api/me` → role, phone name, lab, and for the app: `computer`, `version`, `api` (1) and `addresses` (where the phone listener is: the lab network and Tailscale). The app keeps them and tries the address that worked last first.
- Watch and control: `GET /api/state`, `/api/live?since=n`, `/api/runs`, `/api/runs/{id}`, `/api/runs/{id}/files/{name}`; `POST /api/approvals/{id}` (control only for approving), `/api/alarms/{id}/ack`, `/api/runs/stop`, `/api/stop`, `/api/me/forget`.
- Control: `GET /api/bootstrap` (models), `/api/chats`, `/api/chats/{id}`; `POST /api/chats {model, incognito}`, `/api/chats/{id}/turn {text, model}` (the answer streams back as one JSON event per line).

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

## Next: through the hub

Once the Kelvin hub runs all the time at a public address, the app will connect through it as a relay (so it needs no VPN away from the lab), and the hub can send push notifications through Apple's push service. Until then, alerts while the app is closed come from Kelvin's ntfy webhook (Settings → Notifications).
