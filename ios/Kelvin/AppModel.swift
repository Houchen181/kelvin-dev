import Foundation
import KelvinKit

/// The computers this phone works with (paired, or through the Kelvin account) and which one the app shows.
@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var list: ComputerList
    @Published var pairing = false
    @Published var signingIn = false
    @Published private(set) var account: Account?
    @Published private(set) var kelvins: [HubKelvin] = []     // the account's, as the hub last listed them
    /// The name this phone writes under in team chats: each person their own, also on an account people share.
    @Published var teamName: String {
        didSet { defaults.set(teamName, forKey: "kelvin.teamName") }
    }

    let transport: Transport
    let tokens: TokenStore
    private let defaults: UserDefaults
    private let key = "kelvin.computers"
    private let accountKey = "kelvin.account"
    private var sessions: [UUID: LabSession] = [:]

    init(transport: Transport = URLSessionTransport.shared, defaults: UserDefaults = .standard,
         tokens: TokenStore = Keychain()) {
        self.transport = transport
        self.tokens = tokens
        self.defaults = defaults
        var kept = ComputerList.decode(defaults.data(forKey: key))
        let older = kept.tidy()   // older pairings of a computer paired again, from app 0.1.6 and before
        for id in older { tokens.delete(id) }
        list = kept
        account = Account.decode(defaults.data(forKey: accountKey))
        teamName = defaults.string(forKey: "kelvin.teamName") ?? ""
        if !older.isEmpty { save() }
    }

    /// The account's Kelvins removed on this phone (their ids on the hub): not listed again until the phone signs in
    /// to the account again.
    private var removedKelvins: Set<String> { Set(defaults.stringArray(forKey: "kelvin.removedKelvins") ?? []) }

    /// The newest team chat message shown on this phone, per computer (nil: it never looked).
    func teamRead(_ c: Computer) -> Int? { defaults.object(forKey: "kelvin.teamRead." + c.id.uuidString) as? Int }

    func setTeamRead(_ id: Int, _ c: Computer) { defaults.set(id, forKey: "kelvin.teamRead." + c.id.uuidString) }

    /// This phone's token on the Kelvin hub, while signed in to the account.
    var accountToken: String? { account == nil ? nil : tokens.token(for: Account.tokenID) }

    /// The hub to sign in to: the one Kelvin hub, unless this phone signed in to another (development).
    var lastHub: URL { defaults.string(forKey: "kelvin.hub").flatMap { URL(string: $0) } ?? Account.defaultHub }

    // ------------------------------------------------------------------ the Kelvin account

    /// Sign in with the Kelvin account: keep the hub's token for this phone, and add the account's Kelvins.
    func signIn(hub: URL, email: String, password: String) async throws {
        let id = defaults.string(forKey: "kelvin.phoneID") ?? PhoneInfo.newID()
        defaults.set(id, forKey: "kelvin.phoneID")
        let phone = PhoneInfo(id: id, name: Device.name, version: Device.version)
        let (token, signedIn) = try await HubClient(hub: hub, transport: transport)
            .signIn(email: email.trimmingCharacters(in: .whitespaces), password: password, phone: phone)
        tokens.save(token, for: Account.tokenID)
        account = signedIn
        defaults.set(signedIn.encoded(), forKey: accountKey)
        defaults.set(hub.absoluteString, forKey: "kelvin.hub")
        try? await lookForKelvins()
        Notifier.shared.ask()
    }

    /// Ask the hub which Kelvins the account is signed in to: one row per computer, without those removed here or
    /// those the hub no longer lists (ComputerList.sync).
    func lookForKelvins() async throws {
        guard let account, let token = accountToken else { return }
        do {
            kelvins = try await HubClient(hub: account.hub, token: token, transport: transport).kelvins()
        } catch let e as KelvinError where e.kind == .unpaired {
            kelvins = []
            throw e
        }
        for id in list.sync(kelvins, hub: account.hub, removed: removedKelvins) { forgetHere(id) }
        save()
    }

    /// Sign this phone out of the account: each Kelvin forgets its direct token, the hub forgets this phone.
    func signOut() async {
        guard let account else { return }
        let token = accountToken
        for c in list.computers where c.hub == account.hub && c.viaAccount {
            if let token, let base = c.relayBase {
                _ = try? await KelvinClient(base: base, token: token, transport: transport, timeout: 5, bearer: true)
                    .forgetDirect()
            }
            tokens.delete(c.id)
            sessions[c.id]?.pause()
            sessions[c.id] = nil
            list.remove(c.id)
        }
        if let token { await HubClient(hub: account.hub, token: token, transport: transport).signOut() }
        tokens.delete(Account.tokenID)
        self.account = nil
        kelvins = []
        defaults.removeObject(forKey: accountKey)
        defaults.removeObject(forKey: "kelvin.removedKelvins")   // signed in again, the phone lists them all again
        save()
    }

    /// Delete the Kelvin account on the hub (its password), then sign this phone out of it.
    func deleteAccount(password: String) async throws {
        guard let account else { return }
        try await HubClient(hub: account.hub, transport: transport).deleteAccount(email: account.email, password: password)
        await signOut()
    }

    /// What the hub last said about one of the account's Kelvins.
    func kelvin(_ c: Computer) -> HubKelvin? { kelvins.first { $0.id == c.kelvin } }

    var current: Computer? { list.current }

    func session(for c: Computer) -> LabSession {
        if let s = sessions[c.id] { return s }
        let s = LabSession(computer: c, app: self)
        sessions[c.id] = s
        return s
    }

    /// The demo lab answers inside the app; every other computer over the network.
    func transport(for c: Computer) -> Transport {
        c.addresses.contains(DemoTransport.address) ? DemoTransport.shared : transport
    }

    /// Add the demo lab (again) and show it.
    func startDemo() {
        let c = Computer(lab: "Demo lab", host: "DEMO", phoneName: "This phone", role: "control",
                         addresses: [DemoTransport.address])
        tokens.save("demo", for: c.id)
        if let old = list.add(c) {
            tokens.delete(old)
            sessions[old]?.pause()
            sessions[old] = nil
        }
        save()
    }

    func select(_ id: UUID) {
        list.selected = id
        save()
    }

    /// Offer the pairing code, keep the token, and learn the computer's name and other addresses.
    @discardableResult
    func pair(at base: URL, code: String) async throws -> Computer {
        try await pair(at: [base], code: code)
    }

    /// Pair at the first of `addresses` that answers (a pairing code holds every address Kelvin has).
    @discardableResult
    func pair(at addresses: [URL], code: String) async throws -> Computer {
        let (client, result) = try await reach(addresses, token: nil, transport: transport, timeout: 8) {
            try await $0.pair(code: code)
        }
        let base = client.base
        var c = Computer(lab: base.host ?? "Kelvin", phoneName: result.name, role: result.role,
                         addresses: [base] + addresses.filter { $0 != base })
        tokens.save(result.token, for: c.id)
        if let me = try? await KelvinClient(base: base, token: result.token, transport: transport).me() {
            c.update(with: me, used: base)
        }
        if let old = list.add(c) {
            tokens.delete(old)
            sessions[old]?.pause()
            sessions[old] = nil
        }
        save()
        Notifier.shared.ask()
        return c
    }

    /// A direct token a Kelvin gave this phone through the hub.
    func keep(direct token: String, for c: Computer) { tokens.save(token, for: c.id) }

    /// What a session learned about its computer (its name, the address that worked).
    func update(_ c: Computer) {
        guard list.computers.contains(where: { $0.id == c.id && $0 != c }) else { return }
        list.update(c)
        save()
    }

    /// Forget a computer on this phone, at once. Then, if it answers, Kelvin there forgets this phone too: its pairing,
    /// or, for one of the account's Kelvins, the direct token it gave this phone. One of the account's stays off this
    /// list until the phone signs in to the account again.
    func remove(_ c: Computer) async {
        let token = tokens.token(for: c.id), hubToken = accountToken, here = transport(for: c)
        if c.viaAccount, let kelvin = c.kelvin {
            defaults.set(Array(removedKelvins.union([kelvin])), forKey: "kelvin.removedKelvins")
        }
        forgetHere(c.id)
        list.remove(c.id)
        save()
        if c.viaAccount, let base = c.relayBase, let hubToken {
            _ = try? await KelvinClient(base: base, token: hubToken, transport: here, timeout: 5, bearer: true).forgetDirect()
        } else if !c.viaAccount, let token {
            _ = try? await reach(c.addresses, token: token, transport: here) { try await $0.forget() }
        }
    }

    /// What this phone keeps for a computer that left its list: its token, its session, its team chat read mark.
    private func forgetHere(_ id: UUID) {
        tokens.delete(id)
        sessions[id]?.pause()
        sessions[id] = nil
        defaults.removeObject(forKey: "kelvin.teamRead." + id.uuidString)
    }

    private func save() {
        defaults.set(list.encoded(), forKey: key)
    }
}

/// One computer: its state every few seconds while the app is in front, the run in progress, and every action.
@MainActor
final class LabSession: ObservableObject {
    @Published private(set) var computer: Computer
    @Published private(set) var state: LabState?
    @Published private(set) var problem: KelvinError?
    @Published private(set) var connected: URL?
    @Published private(set) var live = LiveData()
    @Published private(set) var teamRead: Int?     // the newest team chat message shown on this phone

    private weak var app: AppModel?
    private let transport: Transport
    private let tokens: TokenStore
    private var client: KelvinClient?
    private var loop: Task<Void, Never>?
    private var lastTeam: Int?       // the team chat's newest message at the last look (it goes down only when it begins again)
    static let every: UInt64 = 3_000_000_000

    init(computer: Computer, app: AppModel) {
        self.computer = computer
        self.app = app
        transport = app.transport(for: computer)
        tokens = app.tokens
        teamRead = app.teamRead(computer)
    }

    var role: Role { computer.access }
    var route: Route? { connected.map { computer.route($0) } }

    /// Team chat messages written since this phone last showed the chat.
    var teamUnread: Int {
        guard let last = state?.team, let read = teamRead else { return 0 }
        return max(0, last - read)
    }

    /// The team chat was shown up to this message. force: also back (a team chat that began again).
    func readTeam(upTo id: Int, force: Bool = false) {
        guard force || id > (teamRead ?? -1) else { return }
        teamRead = id
        app?.setTeamRead(id, computer)
    }

    /// Look every few seconds until paused.
    func resume() {
        guard loop == nil else { return }
        loop = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                try? await Task.sleep(nanoseconds: LabSession.every)
            }
        }
    }

    func pause() {
        loop?.cancel()
        loop = nil
    }

    func refresh() async {
        do {
            let c = try await ready()
            let s = try await c.state()
            state = s
            problem = nil
            if let last = s.team {   // the first look: what was there is not news; lower than the look before:
                if teamRead == nil || last < (lastTeam ?? last) { readTeam(upTo: last, force: true) }   // begun again
                lastTeam = last
            }
            Notifier.shared.look(at: s, on: computer)
            await follow(s.live, with: c)
        } catch let e as KelvinError {
            if e.kind == .unreachable {
                client = nil
                connected = nil
            }
            problem = e
        } catch {
            problem = KelvinError(.server, error.localizedDescription)
        }
    }

    /// Rows of the run in progress, and once more after it ends.
    private func follow(_ run: RunProgress?, with c: KelvinClient) async {
        guard let run, !run.runID.isEmpty else { return }
        if run.runID == live.runID, !run.running, live.since >= run.done { return }
        let since = run.runID == live.runID ? live.since : 0
        guard let answer = try? await c.live(since: since) else { return }
        if live.take(answer, since: since), let again = try? await c.live(since: 0) {
            live.take(again, since: 0)
        }
    }

    /// A client at an address that answers: the one that worked last, else each address in turn. Through the Kelvin
    /// account: directly first, when the Kelvin gave this phone its own token for that, else through the hub.
    func ready() async throws -> KelvinClient {
        if let client { return client }
        if let relay = computer.relayBase { return try await readyThroughAccount(relay) }
        guard let token = tokens.token(for: computer.id) else {
            throw KelvinError(.unpaired, "This phone is not paired with \(computer.lab) any more. Pair it again.")
        }
        let (c, me) = try await reach(computer.addresses, token: token, transport: transport) { try await $0.me() }
        client = c
        connected = c.base
        computer.update(with: me, used: c.base)
        app?.update(computer)
        return c
    }

    private func readyThroughAccount(_ relay: URL) async throws -> KelvinClient {
        if let direct = tokens.token(for: computer.id), !computer.addresses.isEmpty {
            do {   // on the lab network or Tailscale: faster, and unshared work opens too
                let (c, me) = try await reach(computer.addresses, token: direct, transport: transport, timeout: 2.5) {
                    try await $0.me()
                }
                client = c
                connected = c.base
                computer.update(with: me, used: c.base)
                app?.update(computer)
                return c
            } catch let e as KelvinError where e.kind == .unpaired {
                tokens.delete(computer.id)   // the Kelvin forgot it: ask for another through the hub
            } catch {}
        }
        guard let token = app?.accountToken else {
            throw KelvinError(.unpaired, "Sign in to your Kelvin account again.")
        }
        let c = KelvinClient(base: relay, token: token, transport: transport, timeout: 15, bearer: true)
        let me = try await c.me()
        client = c
        connected = relay
        if let name = me.lab, !name.isEmpty { computer.lab = name }
        if let h = me.computer, !h.isEmpty { computer.host = h }
        if let n = me.name, !n.isEmpty { computer.phoneName = n }
        computer.role = me.role
        computer.version = me.version ?? computer.version
        computer.kinds = (computer.kinds ?? [:]).merging([relay.absoluteString: "hub"]) { _, new in new }
        app?.update(computer)
        if tokens.token(for: computer.id) == nil { await askDirect(c) }
        return c
    }

    /// Through the hub, ask the Kelvin for a token of this phone's own, to reach it directly next time.
    private func askDirect(_ c: KelvinClient) async {
        guard let d = try? await c.direct(), d.available, let token = d.token else { return }
        app?.keep(direct: token, for: computer)
        computer.addresses = d.addresses.compactMap { URL(string: $0) }
        computer.kinds = (computer.kinds ?? [:]).merging(d.kinds) { _, new in new }
        app?.update(computer)
    }

    // ------------------------------------------------------------------ actions

    func answer(_ r: ApprovalRequest, approve: Bool) async throws {
        try await ready().answer(r, approve: approve)
        await refresh()
    }

    func acknowledge(_ a: Alarm) async throws {
        try await ready().acknowledge(a)
        await refresh()
    }

    func stopRun() async throws {
        try await ready().stopRun()
        await refresh()
    }

    func stopChat() async throws { try await ready().stopChat() }

    /// Lock while hands are on the sample, or unlock. Returns what Kelvin did, in words.
    func hands(on: Bool) async throws -> [String] {
        let r = try await ready().hands(on: on)
        await refresh()
        return r.lines
    }

    func runs(limit: Int? = nil) async throws -> [RunSummary] { try await ready().runs(limit: limit) }

    func run(_ id: String) async throws -> RunDetail { try await ready().run(id) }

    /// A run's file, saved where Quick Look can open it.
    func runFile(_ id: String, _ name: String) async throws -> URL {
        let data = try await ready().runFile(id, name)
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(id, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent(name)
        try data.write(to: url, options: .atomic)
        return url
    }

    func models() async throws -> Models { try await ready().models() }

    func chats() async throws -> [ChatSummary] { try await ready().chats() }

    func chat(_ id: String) async throws -> ChatDetail { try await ready().chat(id) }

    func newChat(model: String, incognito: Bool) async throws -> ChatSummary {
        try await ready().newChat(model: model, incognito: incognito)
    }

    func turn(chat id: String, text: String, model: String) async throws -> AsyncThrowingStream<ChatEvent, Error> {
        try await ready().turn(chat: id, text: text, model: model)
    }

    // ------------------------------------------------------------------ the team chat and Analysis

    func team(after: Int) async throws -> TeamPage { try await ready().team(after: after) }

    func team(before: Int) async throws -> TeamPage { try await ready().team(before: before) }

    func postTeam(name: String, text: String) async throws -> TeamMessage {
        try await ready().postTeam(name: name, text: text)
    }

    func analysisColumns(run: String) async throws -> AnalysisColumns { try await ready().analysisColumns(run: run) }

    func analyze(_ spec: AnalysisSpec) async throws -> AnalysisResult { try await ready().analyze(spec) }

    func keepFit(_ spec: AnalysisSpec, curve: Int, replace: Bool) async throws -> KeepAnswer {
        try await ready().keepFit(spec, curve: curve, replace: replace)
    }

    /// The curves as a CSV file, saved where Quick Look can open and share it.
    func analysisCSV(_ spec: AnalysisSpec) async throws -> URL {
        let (name, data) = try await ready().analysisCSV(spec)
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("analysis", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent(name)
        try data.write(to: url, options: .atomic)
        return url
    }
}

/// One conversation on the phone.
@MainActor
final class ChatModel: ObservableObject {
    @Published private(set) var transcript: Transcript
    @Published var draft = ""
    @Published private(set) var problem: String?

    let session: LabSession
    private(set) var id: String?
    private var model: String?

    init(session: LabSession, chat: ChatSummary?, incognito: Bool = false) {
        self.session = session
        id = chat?.id
        model = chat?.model
        transcript = Transcript(title: chat?.title ?? (incognito ? "Incognito chat" : "New chat"), model: chat?.model,
                                incognito: chat?.incognito ?? incognito)
    }

    /// The conversation as Kelvin keeps it (also after the phone lost an answer that went on without it).
    func load() async {
        guard let id, !transcript.answering else { return }
        do {
            let chat = try await session.chat(id)
            transcript = Transcript(chat)
            model = chat.model ?? model
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
    }

    @discardableResult
    func send() -> Task<Void, Never>? {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !transcript.answering else { return nil }
        draft = ""
        problem = nil
        transcript.say(text)
        return Task { await self.answer(text) }
    }

    private func answer(_ text: String) async {
        do {
            if id == nil || model == nil {
                let models = try await session.models()
                guard let m = model ?? models.defaultModel ?? models.all.first?.key else {
                    throw KelvinError(.server, "Kelvin has no model to answer with. Add a model's key on the Kelvin computer.")
                }
                model = m
            }
            if id == nil {
                id = try await session.newChat(model: model ?? "", incognito: transcript.incognito).id
            }
            for try await event in try await session.turn(chat: id ?? "", text: text, model: model ?? "") {
                transcript.apply(event)
            }
            transcript.finish()
        } catch let e as KelvinError where e.kind == .unreachable {
            transcript.finish()
            problem = "Lost the connection. Kelvin goes on with the answer on its computer; pull down to load it."
        } catch {
            transcript.apply(.error(error.localizedDescription))
            transcript.finish()
        }
    }

    func stop() async {
        do {
            try await session.stopChat()
        } catch {
            problem = error.localizedDescription
        }
    }

    func answer(_ r: ApprovalRequest, approve: Bool) async {
        do {
            try await session.answer(r, approve: approve)
            transcript.answered(r.id, approve: approve)
        } catch {
            problem = error.localizedDescription
        }
    }
}
