import KelvinKit
import SwiftUI

/// The Kelvin computers this phone is paired with: switch between them, pair another, unpair.
struct ComputersView: View {
    @ObservedObject var session: LabSession
    @EnvironmentObject var app: AppModel
    @State var removing: Computer?

    var body: some View {
        List {
            Section {
                ForEach(app.list.computers) { c in
                    Button {
                        app.select(c.id)
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(c.title(among: app.list.computers)).foregroundStyle(Color.primary)
                                Text([c.host, c.access == .watch ? "watch" : "control"].compactMap { $0 }.joined(separator: " · "))
                                    .font(.caption).foregroundStyle(Color.secondary)
                            }
                            Spacer()
                            if c.id == app.current?.id { Image(systemName: "checkmark").foregroundStyle(Color.accentColor) }
                        }
                    }
                    .swipeActions {
                        Button("Unpair", role: .destructive) { removing = c }
                    }
                }
                Button {
                    app.pairing = true
                } label: {
                    Label("Pair with a Kelvin computer", systemImage: "qrcode.viewfinder")
                }
            } footer: {
                Text("Swipe left to unpair one.")
            }

            let c = session.computer
            Section("This phone on \(c.lab)") {
                LabeledContent("Name", value: c.phoneName)
                Text(c.access.summary).foregroundStyle(.secondary)
                if let v = c.version { LabeledContent("Kelvin", value: v) }
                RouteLabel(url: session.connected)
            }
            if c.addresses != [DemoTransport.address] {
            Section {
                ForEach(c.addresses, id: \.self) { url in
                    HStack {
                        Text(url.absoluteString).font(.footnote.monospaced())
                        Spacer()
                        Text(Route(url).rawValue).font(.caption).foregroundStyle(.secondary)
                    }
                }
            } header: {
                Text("Addresses")
            } footer: {
                Text("Kelvin tries these in turn, the one that worked last first. On a shared network prefer Tailscale: "
                     + "plain Wi-Fi traffic is not encrypted.")
            }
            }
            Section("Notifications") {
                Text("Kelvin tells you about new requests and alarms while this app is open. For alerts when it is "
                     + "closed, set up ntfy under Notifications on the Kelvin computer.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .navigationTitle("Computers")
        .confirmationDialog("Unpair \(removing?.lab ?? "")?", isPresented: Binding(get: { removing != nil },
                                                                                   set: { if !$0 { removing = nil } }),
                            titleVisibility: .visible) {
            Button("Unpair", role: .destructive) {
                if let c = removing { Task { await app.remove(c) } }
            }
        } message: {
            Text("This phone stops working with that computer, and Kelvin there forgets it. Pairing again needs a new code.")
        }
    }
}
