# Kelvin for iPhone

A native iPhone and iPad app that is a remote for Kelvin on a lab computer: the lab at a glance, Kelvin's requests to approve or decline, the run in progress with a live plot and **Stop**, chats, and past runs with their reports. It talks to Kelvin directly, on the lab network or through Tailscale; nothing goes through anyone else's server.

How people use it is in [portal.md](portal.md#phone-and-remote-access). This page is for building it.

## What's where

| Path | What |
|---|---|
| `ios/project.yml` | The Xcode project, as an [XcodeGen](https://github.com/yonaskolb/XcodeGen) spec. `Kelvin.xcodeproj` and `Support/Info.plist` are generated from it and not committed. |
| `ios/KelvinKit/` | A Swift package with no UI and only Foundation: pairing links, the phone API client, the models, the chat stream and transcript, plot data. Its tests run on Linux too. |
| `ios/Kelvin/` | The SwiftUI app: `AppModel.swift` (paired computers, one `LabSession` per computer, `ChatModel`), `Platform.swift` (URLSession, Keychain, notifications) and the screens. |
| `ios/KelvinTests/` | App tests: pairing, the Keychain, falling back to the Tailscale address, a chat, notifications. |
| `.github/workflows/ios.yml` | CI: KelvinKit's tests on Linux; when they pass, the app's build and tests on the iOS Simulator (macOS runner). |

The app needs iOS 16. It uses only Apple's frameworks (SwiftUI, Charts, AVFoundation for the QR scanner, QuickLook for reports).

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

## TestFlight and the App Store

Needs an Apple Developer Program membership (99 USD a year) and a Mac with the current Xcode, or a CI job that signs:

1. In App Store Connect, add the app with the bundle id `io.github.houchen181.kelvin` (or change `PRODUCT_BUNDLE_IDENTIFIER` to one of yours).
2. Set `DEVELOPMENT_TEAM`, raise `CURRENT_PROJECT_VERSION` for each upload, then Product → Archive → Distribute App → TestFlight.
3. For external testers, Apple's beta review asks why the app uses plain http: it connects to the user's own lab computer on the local network or inside Tailscale (which encrypts the traffic), and never to a public server.

## Security notes

- The token is in the Keychain (this device only, after first unlock); the list of computers in UserDefaults holds nothing secret.
- App Transport Security: from iOS 17 the app needs `NSAllowsLocalNetworking` to reach a lab IP address, and exceptions for Tailscale's `100.64.0.0/10` and `*.ts.net`. Any other plain-http address is refused by iOS.
- Unpairing in the app also unpairs the phone on the Kelvin computer when it answers.

## Next: through the hub

Once the Kelvin hub runs all the time at a public address, the app will connect through it as a relay (so it needs no VPN away from the lab), and the hub can send push notifications through Apple's push service. Until then, alerts while the app is closed come from Kelvin's ntfy webhook (Settings → Notifications).
