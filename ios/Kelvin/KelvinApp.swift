import KelvinKit
import SwiftUI
import UserNotifications

@main
struct KelvinApp: App {
    @StateObject var app = AppModel()

    init() {
        UNUserNotificationCenter.current().delegate = Notifier.shared
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
        }
    }
}

struct RootView: View {
    @EnvironmentObject var app: AppModel

    var body: some View {
        Group {
            if let c = app.current {
                LabTabs(session: app.session(for: c))
                    .id(c.id)
            } else {
                WelcomeView()
            }
        }
        .sheet(isPresented: $app.pairing) {
            PairView()
                .environmentObject(app)
        }
    }
}

/// One computer's tabs. The lab is polled while the app is in front.
struct LabTabs: View {
    @ObservedObject var session: LabSession
    @Environment(\.scenePhase) var phase

    var body: some View {
        TabView {
            NavigationStack { LabView(session: session) }
                .tabItem { Label("Lab", systemImage: "gauge.with.dots.needle.33percent") }
                .badge(session.state?.waiting ?? 0)
            if session.role.canChat {
                NavigationStack { ChatListView(session: session) }
                    .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right") }
            }
            NavigationStack { RunsView(session: session) }
                .tabItem { Label("Runs", systemImage: "chart.xyaxis.line") }
            NavigationStack { ComputersView(session: session) }
                .tabItem { Label("Computers", systemImage: "desktopcomputer") }
        }
        .onAppear { session.resume() }
        .onDisappear { session.pause() }
        .onChange(of: phase) { now in
            if now == .active { session.resume() } else { session.pause() }
        }
    }
}

struct WelcomeView: View {
    @EnvironmentObject var app: AppModel

    var body: some View {
        VStack(spacing: 18) {
            Spacer()
            Image(systemName: "thermometer.snowflake")
                .font(.system(size: 64))
                .foregroundStyle(Color.accentColor)
            Text("Kelvin")
                .font(.largeTitle.bold())
            Text("Watch your lab, answer Kelvin's requests and stop a run from your phone. The app talks to Kelvin on "
                 + "your lab computer directly, on the lab network or through Tailscale.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            Button {
                app.pairing = true
            } label: {
                Label("Pair with a Kelvin computer", systemImage: "qrcode.viewfinder")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            Spacer()
            Text("On the Kelvin computer, open Settings → Phone, switch on Phone access, and press Pair a phone.")
                .font(.footnote)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
        }
        .padding(24)
    }
}

// ------------------------------------------------------------------ small shared pieces

/// Runs an action and shows what went wrong, if anything.
struct Failure: ViewModifier {
    @Binding var message: String?

    func body(content: Content) -> some View {
        content.alert("Kelvin", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(message ?? "")
        }
    }
}

extension View {
    func failure(_ message: Binding<String?>) -> some View { modifier(Failure(message: message)) }
}

func attempt(_ failure: Binding<String?>, _ work: @escaping @MainActor () async throws -> Void) {
    Task { @MainActor in
        do {
            try await work()
        } catch {
            failure.wrappedValue = error.localizedDescription
        }
    }
}

enum Format {
    static func duration(_ seconds: Double) -> String {
        if seconds < 90 { return "\(Int(seconds.rounded())) s" }
        if seconds < 90 * 60 { return "\(Int((seconds / 60).rounded())) min" }
        return String(format: "%.1f h", seconds / 3600)
    }

    static func time(_ epoch: Double) -> String {
        Date(timeIntervalSince1970: epoch).formatted(date: .omitted, time: .shortened)
    }

    static func progress(_ run: RunProgress) -> String {
        var s = "\(run.done) of \(run.total) points"
        if let eta = run.etaSeconds, eta > 0 { s += " · about \(duration(eta)) left" }
        return s
    }
}

/// Where the connection goes, as a small label: green on Tailscale, orange on a plain network.
struct RouteLabel: View {
    let url: URL?

    var body: some View {
        if let url {
            let route = Route(url)
            Label("\(route.rawValue) · \(url.host ?? "")",
                  systemImage: route.encrypted ? "lock.fill" : "wifi")
                .font(.caption)
                .foregroundStyle(route.encrypted ? Color.green : Color.secondary)
        }
    }
}
