// swift-tools-version:5.9
// What the Kelvin iPhone app knows about Kelvin, without any UI: the phone API, the Kelvin account, the chat stream.
// Foundation only, so its tests also run on Linux (see .github/workflows/ios.yml).
import PackageDescription

let package = Package(
    name: "KelvinKit",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [.library(name: "KelvinKit", targets: ["KelvinKit"])],
    targets: [
        .target(name: "KelvinKit"),
        .testTarget(name: "KelvinKitTests", dependencies: ["KelvinKit"]),
    ]
)
