import KelvinKit
import SwiftUI

/// The Kelvin computers this phone works with, paired or through the Kelvin account: switch between them, pair
/// another, unpair, sign in or out.
struct ComputersView: View {
    @ObservedObject var session: LabSession
    @EnvironmentObject var app: AppModel
    @State var removing: Computer?

    var body: some View {
        List {
            AccountSection()
            Section {
                ForEach(app.list.computers) { c in
                    Button {
                        app.select(c.id)
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(c.title(among: app.list.computers)).foregroundStyle(Color.primary)
                                Text(subtitle(c)).font(.caption).foregroundStyle(Color.secondary)
                            }
                            Spacer()
                            if c.id == app.current?.id { Image(systemName: "checkmark").foregroundStyle(Color.accentColor) }
                        }
                    }
                    .swipeActions {
                        Button(c.viaAccount ? "Remove" : "Unpair", role: .destructive) { removing = c }
                    }
                }
                Button {
                    app.pairing = true
                } label: {
                    Label("Pair with a Kelvin computer", systemImage: "qrcode.viewfinder")
                }
            } footer: {
                Text("Swipe left to unpair or remove one.")
            }

            let c = session.computer
            Section("This phone on \(c.lab)") {
                LabeledContent("Name", value: c.phoneName)
                Text(c.access.summary).foregroundStyle(.secondary)
                if let v = c.version { LabeledContent("Kelvin", value: v) }
                RouteLabel(url: session.connected, kinds: session.computer.kinds)
            }
            if c.addresses != [DemoTransport.address] {
            Section {
                ForEach(c.addresses, id: \.self) { url in
                    HStack {
                        Text(url.absoluteString).font(.footnote.monospaced())
                        Spacer()
                        Text(c.route(url).rawValue).font(.caption).foregroundStyle(.secondary)
                    }
                }
            } header: {
                Text("Addresses")
            } footer: {
                Text(c.viaAccount
                     ? "Your phone tries these first, directly (on the lab network or through Tailscale), and goes "
                       + "through the Kelvin hub when none answers."
                     : "Kelvin tries these in turn, the one that worked last first. On a shared network prefer "
                       + "Tailscale: plain Wi-Fi traffic is not encrypted.")
            }
            }
            Section("Notifications") {
                Text("Kelvin tells you about new requests and alarms while this app is open. For alerts when it is "
                     + "closed, set up ntfy under Notifications on the Kelvin computer.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .navigationTitle("Computers")
        .refreshable { try? await app.lookForKelvins() }
        .task { try? await app.lookForKelvins() }
        .confirmationDialog(removeTitle,
                            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
                            titleVisibility: .visible) {
            if let c = removing {
                Button(c.viaAccount ? "Remove" : "Unpair", role: .destructive) { Task { await app.remove(c) } }
            }
        } message: {
            Text(removing?.viaAccount == true
                 ? "It shows again after you sign in to your Kelvin account again."
                 : "This phone stops working with that computer, and Kelvin there forgets it. Pairing again needs a new code.")
        }
    }

    var removeTitle: String {
        guard let c = removing else { return "" }
        return c.viaAccount ? "Remove \(c.lab) from this phone?" : "Unpair \(c.lab)?"
    }

    func subtitle(_ c: Computer) -> String {
        if c.viaAccount {
            let k = app.kelvin(c)
            return [c.host, k.map { $0.online ? "online" : "offline" }, "Kelvin account"].compactMap { $0 }
                .joined(separator: " · ")
        }
        return [c.host, c.access == .watch ? "watch" : "control"].compactMap { $0 }.joined(separator: " · ")
    }
}
