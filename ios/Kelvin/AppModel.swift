import Foundation
import KelvinKit

/// The computers this phone works with (through the Kelvin account, or paired by an app before 0.1.8) and which one
/// the app shows.
@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var list: ComputerList
    @Published var signingIn = false
    @Published private(set) var account: Account?
    @Published private(set) var kelvins: [HubKelvin] = []     // the account's, as the hub last listed them
    /// The name this phone writes under in team chats: each person their own, also on an account people share.
    @Published var teamName: String {
        didSet { defaults.set(teamName, forKey: "kelvin.teamName") }
    }
    /// The AI companies the person let this phone's chats go to: each asked once, before a chat's first message
    /// would go there.
    @Published private(set) var aiAllowed: [String] {
        didSet { defaults.set(aiAllowed, forKey: "kelvin.aiAllowed") }
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
        aiAllowed = defaults.stringArray(forKey: "kelvin.aiAllowed") ?? []
        if !older.isEmpty { save() }
    }

    func allowsAI(_ s: AIService) -> Bool { aiAllowed.contains(s.name) }

    func allowAI(_ s: AIService) {
        if !allowsAI(s) { aiAllowed.append(s.name) }
    }

    /// Ask again before a chat goes to any AI company.
    func forgetAIAllowed() { aiAllowed = [] }

    /// The account's Kelvins removed on this phone (their ids on the hub): not listed again until the phone signs in
    /// to the account again.
    private var removedKelvins: Set<String> { Set(defaults.stringArray(forKey: "kelvin.removedKelvins") ?? []) }

    /// The computers removed on this phone, by name (app 0.1.9 on): none of their Kelvins is listed again, older copies
    /// included, until Look for my Kelvins or signing in again brings them back. The ids removed before hide only that
    /// one Kelvin: some were older copies of a computer still in use.
    private var removedNames: Set<String> { Set(defaults.stringArray(forKey: "kelvin.removedNames") ?? []) }

    /// Every computer removed on this phone, listed again.
    private func forgetRemoved() {
        defaults.removeObject(forKey: "kelvin.removedKelvins")
        defaults.removeObject(forKey: "kelvin.removedNames")
    }

    /// The newest team chat message shown on this phone, per computer (nil: it never looked).
    func teamRead(_ c: Computer) -> Int? { defaults.object(forKey: "kelvin.teamRead." + c.id.uuidString) as? Int }

    func setTeamRead(_ id: Int, _ c: Computer) { defaults.set(id, forKey: "kelvin.teamRead." + c.id.uuidString) }

    /// This phone's token on the Kelvin hub, while signed in to the account.
    var accountToken: String? { account == nil ? nil : tokens.token(for: Account.tokenID) }

    /// The hub to sign in to: the one Kelvin hub, unless this phone signed in to another (development).
    var lastHub: URL { defaults.string(forKey: "kelvin.hub").flatMap { URL(string: $0) } ?? Account.defaultHub }

    /// Signing out of the account, or deleting it: each Kelvin is told to forget this phone's direct token, so none
    /// is asked for another meanwhile.
    private(set) var leaving = false

    // ------------------------------------------------------------------ the Kelvin account

    /// Sign in with the Kelvin account: keep the hub's token for this phone, and add the account's Kelvins.
    func signIn(hub: URL, email: String, password: String) async throws {
        let id = defaults.string(forKey: "kelvin.phoneID") ?? PhoneInfo.newID()
        defaults.set(id, forKey: "kelvin.phoneID")
        let phone = PhoneInfo(id: id, name: Device.name, version: Device.version)
        let (token, signedIn) = try await HubClient(hub: hub, transport: transport)
            .signIn(email: email.trimmingCharacters(in: .whitespaces), password: password, phone: phone)
        tokens.save(token, for: Account.tokenID)
        for s in sessions.values { s.reconnect() }   // the account's computers: with this token from now on
        account = signedIn
        defaults.set(signedIn.encoded(), forKey: accountKey)
        defaults.set(hub.absoluteString, forKey: "kelvin.hub")
        try? await lookForKelvins()
        Notifier.shared.ask()
    }

    /// Ask the hub which Kelvins the account is signed in to: one row per computer, without those removed here or
    /// those the hub no longer lists (ComputerList.sync). all: a person asked (Look for my Kelvins), so the computers
    /// removed on this phone come back too.
    func lookForKelvins(all: Bool = false) async throws {
        guard let account, let token = accountToken else { return }
        do {
            kelvins = try await HubClient(hub: account.hub, token: token, transport: transport).kelvins()
        } catch let e as KelvinError where e.kind == .unpaired {
            kelvins = []
            throw e
        }
        if all { forgetRemoved() }
        for id in list.sync(kelvins, hub: account.hub, removed: removedKelvins, removedNames: removedNames) {
            forgetHere(id)
        }
        save()
    }

    /// Sign this phone out of the account: each Kelvin forgets its direct token, the hub forgets this phone.
    func signOut() async {
        guard let account else { return }
        leaving = true
        defer { leaving = false }
        let token = accountToken
        await forgetDirect(account)
        for c in list.computers where c.hub == account.hub && c.viaAccount {
            tokens.delete(c.id)
            sessions[c.id]?.pause()
            sessions[c.id] = nil
            list.remove(c.id)
        }
        list.forgetAccount(account.hub)
        if let token { await HubClient(hub: account.hub, token: token, transport: transport).signOut() }
        tokens.delete(Account.tokenID)
        self.account = nil
        kelvins = []
        defaults.removeObject(forKey: accountKey)
        forgetRemoved()   // signed in again, the phone lists them all again
        save()
    }

    /// Delete the Kelvin account on the hub (its password), then sign this phone out of it. Each Kelvin forgets this
    /// phone's direct token first, while the hub still takes the phone to it: after, nothing could tell it, and it
    /// would count the phone among those that answer from away for good (its requests would wait for a gone phone).
    func deleteAccount(password: String) async throws {
        guard let account else { return }
        leaving = true
        defer { leaving = false }
        await forgetDirect(account)
        try await HubClient(hub: account.hub, transport: transport).deleteAccount(email: account.email, password: password)
        await signOut()
    }

    /// Each of the account's Kelvins forgets the direct token it gave this phone, through the hub.
    private func forgetDirect(_ account: Account) async {
        guard let token = accountToken else { return }
        for c in list.computers where c.hub == account.hub && c.viaAccount {
            guard let base = c.relayBase else { continue }
            _ = try? await KelvinClient(base: base, token: token, transport: transport, timeout: 5, bearer: true)
                .forgetDirect()
        }
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
            if let name = c.host?.lowercased() {   // the computer: its older Kelvins stay away too
                defaults.set(Array(removedNames.union([name])), forKey: "kelvin.removedNames")
            }
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
    private(set) var stateAsked = Date.distantPast   // when the state shown was asked for (looks can overlap)

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

    func allowsAI(_ s: AIService) -> Bool { app?.allowsAI(s) ?? false }

    func allowAI(_ s: AIService) { app?.allowAI(s) }

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

    /// Look every few seconds until paused. The first look after the app was away is on screen: no sound for it.
    func resume() {
        guard loop == nil else { return }
        Notifier.shared.returned(to: computer)
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

    /// Find the computer again at the next look: where it answers, and with the token there is then (the account
    /// signed in again, say).
    func reconnect() {
        client = nil
        connected = nil
    }

    func refresh() async {
        do {
            let c = try await ready()
            let asked = Date()
            let s = try await c.state()
            stateAsked = asked
            state = s
            problem = nil
            if let last = s.team {   // the first look: what was there is not news; lower than the look before:
                if teamRead == nil || last < (lastTeam ?? last) { readTeam(upTo: last, force: true) }   // begun again
                lastTeam = last
            }
            Notifier.shared.look(at: s, on: computer)
            await follow(s.live, with: c)
        } catch let e as KelvinError {
            if e.kind == .unreachable || e.kind == .unpaired { reconnect() }   // a token refused may be replaced
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
            throw KelvinError(.unpaired, "Kelvin on \(computer.lab) no longer knows this phone.")
        }
        let reached: (KelvinClient, Me)
        do {
            reached = try await reach(computer.addresses, token: token, transport: transport) { try await $0.me() }
        } catch let e as KelvinError where e.kind == .unreachable {
            // away from the lab: through the hub, when this phone's Kelvin account reaches the computer too
            guard let relay = app?.list.computers.first(where: { $0.id == computer.id })?.accountRelay,
                  let account = app?.accountToken else { throw e }
            let c = KelvinClient(base: relay, token: account, transport: transport, timeout: 15, bearer: true)
            _ = try await c.me()
            client = c
            connected = relay
            computer.kinds = (computer.kinds ?? [:]).merging([relay.absoluteString: "hub"]) { _, new in new }
            app?.update(computer)
            return c
        }
        let (found, me) = reached
        let c = KelvinClient(base: found.base, token: token, transport: transport)   // the usual wait, not the probe's
        client = c
        connected = c.base
        computer.update(with: me, used: c.base)
        app?.update(computer)
        return c
    }

    private func readyThroughAccount(_ relay: URL) async throws -> KelvinClient {
        let addresses = computer.directAddresses   // never one on the internet: plain http there
        if let direct = tokens.token(for: computer.id), !addresses.isEmpty {
            do {   // on the lab network or Tailscale: faster, and unshared work opens too
                let (found, me) = try await reach(addresses, token: direct, transport: transport, timeout: 2.5) {
                    try await $0.me()
                }
                let c = KelvinClient(base: found.base, token: direct, transport: transport)   // the usual wait now
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
        if tokens.token(for: computer.id) == nil, app?.leaving == false { await askDirect(c) }
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

    func stopChat(_ id: String) async throws { try await ready().stopChat(id) }

    /// Lock while hands are on the sample, or unlock. Returns what Kelvin did, in words.
    func hands(on: Bool) async throws -> [String] {
        let r = try await ready().hands(on: on)
        await refresh()
        return r.lines
    }

    func runs(limit: Int? = nil) async throws -> [RunSummary] { try await ready().runs(limit: limit) }

    func run(_ id: String) async throws -> RunDetail { try await ready().run(id) }

    /// A run's file, saved where Quick Look can open it: under the last part of its run's id and of its name, so a
    /// name with "../" in it can't lead out of the app's temporary folder.
    func runFile(_ id: String, _ name: String) async throws -> URL {
        let data = try await ready().runFile(id, name)
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent(KelvinClient.lastPart(id) ?? "run", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent(KelvinClient.lastPart(name) ?? "file")
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

/// What the app asks before a chat's first message goes to an AI company: which company, which model, which computer.
struct AIAsk: Error, Equatable {
    let service: AIService
    let model: String
    let computer: String

    var question: String { service.question }
    var explanation: String { service.explanation(model: model, computer: computer) }
}

/// One conversation on the phone.
@MainActor
final class ChatModel: ObservableObject {
    @Published private(set) var transcript: Transcript
    @Published var draft = ""
    @Published private(set) var problem: String?
    /// The message would go to an AI company this phone hasn't been allowed to send chats to: the app asks first.
    @Published private(set) var asking: AIAsk?

    let session: LabSession
    private(set) var id: String?
    private var model: String?
    private var sending = false      // the message is on its way: Kelvin doesn't have it yet
    private var stopWanted = false   // Stop pressed meanwhile: sent once Kelvin has the message
    private var ended: Date?         // when this phone saw Kelvin's answer end: a state asked before still has it on

    init(session: LabSession, chat: ChatSummary?, incognito: Bool = false) {
        self.session = session
        id = chat?.id
        model = chat?.model
        transcript = Transcript(title: chat?.title ?? (incognito ? "Incognito chat" : "New chat"), model: chat?.model,
                                incognito: chat?.incognito ?? incognito)
    }

    /// Kelvin answers this chat on its computer while this phone doesn't follow the answer: the chat was opened
    /// while it answered, or the connection was lost. It shows as working, with Stop, and loads again once over.
    var answeringElsewhere: Bool {
        guard let id, !transcript.answering, session.state?.answers(id) == true else { return false }
        return ended.map { session.stateAsked > $0 } ?? true
    }

    /// An answer is under way: the one this phone follows, or one Kelvin goes on with without it.
    var working: Bool { transcript.answering || answeringElsewhere }

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
        guard !text.isEmpty, !working else { return nil }
        draft = ""
        problem = nil
        sending = true
        stopWanted = false
        transcript.say(text)
        return Task { await self.answer(text) }
    }

    private func answer(_ text: String) async {
        var reached = false   // Kelvin has the message
        defer { sending = false }
        do {
            var models: Models?
            if id == nil || model == nil {
                let list = try await session.models()
                guard let m = model ?? list.defaultModel ?? list.all.first?.key else {
                    throw KelvinError(.server, "Kelvin has no model to answer with. Add a model's key on the Kelvin computer.")
                }
                model = m
                models = list
            }
            try await mayGo(models)
            if id == nil {
                id = try await session.newChat(model: model ?? "", incognito: transcript.incognito).id
            }
            let events = try await session.turn(chat: id ?? "", text: text, model: model ?? "")
            reached = true
            sending = false
            if stopWanted {   // Stop was pressed before Kelvin had the message: now there is an answer to stop
                stopWanted = false
                await stop()
            }
            for try await event in events {
                transcript.apply(event)
            }
            transcript.finish()
            ended = Date()
        } catch let wait as AIAsk {   // asked first: the message waits in the box
            transcript.unsay()
            if draft.isEmpty { draft = text }
            asking = wait
        } catch let e as KelvinError where e.kind == .unreachable && reached {
            transcript.lost()   // Kelvin goes on without this phone: what it has of the answer now, or once it's over
            if !answeringElsewhere { await load() }
            problem = "Lost the connection. Kelvin goes on with the answer on its computer; it shows here once it's "
                + "over (or pull down to load it)."
        } catch let error where !reached {   // refused, or it never got there: back in the box, to send again
            transcript.unsay()
            if draft.isEmpty { draft = text }
            problem = error.localizedDescription + " Not sent."
        } catch {
            transcript.apply(.error(error.localizedDescription))
            transcript.finish()
        }
    }

    /// Throws (an AIAsk) when this chat's model would take the message to an AI company the person hasn't let this
    /// phone's chats go to: nothing reaches Kelvin before they say so. models: Kelvin's list, if already asked.
    private func mayGo(_ known: Models?) async throws {
        let key = model ?? ""
        guard let first = AIService.of(model: key, service: known?.service(of: key)), !session.allowsAI(first) else {
            return
        }
        var models = known
        if models == nil { models = try? await session.models() }   // the model's name; a service the person added
        let service = AIService.of(model: key, service: models?.service(of: key)) ?? first
        guard !session.allowsAI(service) else { return }
        throw AIAsk(service: service, model: models?.label(key) ?? key,
                    computer: session.computer.host ?? session.computer.lab)
    }

    /// The person allowed the company: kept on this phone, and the message goes.
    @discardableResult
    func allow() -> Task<Void, Never>? {
        guard let ask = asking else { return nil }
        asking = nil
        session.allowAI(ask.service)
        return send()
    }

    /// Not now: the message stays in the box, unsent.
    func decline() {
        guard let ask = asking else { return }
        asking = nil
        problem = "Not sent. This chat's answers come from \(ask.service.name)."
    }

    func stop() async {
        if sending {   // Kelvin doesn't have the message yet, so nothing there to stop: stopped once it does
            stopWanted = true
            return
        }
        guard let id else { return }
        do {
            try await session.stopChat(id)
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
