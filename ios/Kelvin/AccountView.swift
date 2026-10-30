import KelvinKit
import SwiftUI

/// Sign in with the Kelvin account: the same email and password as on the lab's Kelvin computers. The phone then
/// reaches each of them through the Kelvin hub, with no pairing.
struct AccountView: View {
    @EnvironmentObject var app: AppModel
    @Environment(\.dismiss) var dismiss
    @Environment(\.openURL) var openURL
    @State var hub = ""
    @State var otherHub = false        // the one Kelvin hub, unless the person names another (development)
    @State var email = ""
    @State var password = ""
    @State var working = false
    @State var failure: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Email", text: $email)
                        .keyboardType(.emailAddress)
                        .textContentType(.username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("Password", text: $password)
                        .textContentType(.password)
                } footer: {
                    Text("Your Kelvin account: the one you sign in to Kelvin with on your lab computer. One account "
                         + "works in every lab.")
                }
                if otherHub {
                    Section {
                        TextField("Kelvin hub, e.g. kelvin.example.org", text: $hub)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    } footer: {
                        Text("Only for a test hub: every Kelvin user signs in to the same Kelvin hub.")
                    }
                }
                Section {
                    Button("Sign in") { Task { await signIn() } }
                        .disabled(working || email.isEmpty || password.isEmpty || chosen == nil)
                    if let url = chosen {
                        Button("Create a Kelvin account") { openURL(url.appendingPathComponent("register")) }
                        Button("Forgot your password?") { openURL(url.appendingPathComponent("reset")) }
                    }
                }
                if working {
                    Section { HStack(spacing: 10) { ProgressView(); Text("Signing in…") } }
                }
                if let failure {
                    Section { Text(failure).foregroundStyle(.red) }
                }
                if !otherHub {
                    Section {
                        Button("Use a test hub") { otherHub = true }
                            .font(.footnote)
                    } footer: {
                        Text("Kelvin hub: \(Account.defaultHub.host ?? "")")
                    }
                }
                Section {
                    Text("Your phone then reaches every Kelvin where you are signed in with this account, from any "
                         + "network, through the hub. The hub passes your requests through and keeps none of them. "
                         + "Work marked unpublished or confidential, and incognito chats, open only when your phone "
                         + "reaches the computer directly: on the lab network or through Tailscale.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Kelvin account")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        password = ""   // a password left typed in makes iOS offer to save it
                        dismiss()
                    }
                }
            }
            .onAppear {
                if hub.isEmpty { hub = app.lastHub.absoluteString.replacingOccurrences(of: "https://", with: "") }
                otherHub = app.lastHub != Account.defaultHub
            }
        }
    }

    /// The hub this sign-in goes to.
    var chosen: URL? { otherHub ? Account.hubAddress(hub) : .some(Account.defaultHub) }

    @MainActor
    func signIn() async {
        guard let url = chosen else {
            failure = "That is not a hub address. Type it as Kelvin shows it, e.g. kelvin.example.org."
            return
        }
        working = true
        failure = nil
        defer { working = false }
        do {
            try await app.signIn(hub: url, email: email, password: password)
            password = ""
            dismiss()
        } catch {
            failure = error.localizedDescription
        }
    }
}

/// The account in the Computers tab: who is signed in, the account's Kelvins, signing out and deleting the account.
struct AccountSection: View {
    @EnvironmentObject var app: AppModel
    @State var confirming = false
    @State var looking = false
    @State var looked = false
    @State var deleting = false
    @State var password = ""
    @State var failure: String?

    var body: some View {
        if let a = app.account {
            Section {
                LabeledContent("Signed in", value: a.email)
                LabeledContent("Kelvin hub", value: a.hub.host ?? a.hub.absoluteString)
                Button(looking ? "Looking…" : "Look for my Kelvins") {
                    looking = true
                    attempt($failure) {
                        defer { looking = false }
                        try await app.lookForKelvins()
                        looked = true
                    }
                }
                .disabled(looking)
                Button("Sign out", role: .destructive) { confirming = true }
                Button("Delete Kelvin account…", role: .destructive) {
                    password = ""
                    deleting = true
                }
            } header: {
                Text("Kelvin account")
            } footer: {
                Text(looked && app.kelvins.isEmpty ? "No Kelvin is signed in with this account yet."
                                                   : "Your Kelvins appear here once you sign in to them with this account.")
            }
            .confirmationDialog("Sign out of \(a.email)?", isPresented: $confirming, titleVisibility: .visible) {
                Button("Sign out", role: .destructive) { Task { await app.signOut() } }
            } message: {
                Text("This phone stops reaching your Kelvins through the hub. Paired computers stay.")
            }
            .alert("Delete your Kelvin account?", isPresented: $deleting) {
                SecureField("Password", text: $password)
                Button("Delete", role: .destructive) {
                    let typed = password
                    password = ""
                    attempt($failure) { try await app.deleteAccount(password: typed) }
                }
                Button("Cancel", role: .cancel) { password = "" }
            } message: {
                Text("This deletes \(a.email) on the Kelvin hub, for every computer and phone, and can't be undone. "
                     + "Each Kelvin computer keeps your chats and measurements until you remove the account there too.")
            }
            .failure($failure)
        } else {
            Section {
                Button {
                    app.signingIn = true
                } label: {
                    Label("Sign in with your Kelvin account", systemImage: "person.crop.circle")
                }
            } footer: {
                Text("No pairing: your phone reaches every Kelvin where you are signed in with that account.")
            }
        }
    }
}
