import Foundation
import KelvinKit

/// The paired computers and which one the app shows.
@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var list: ComputerList
    @Published var pairing = false

    let transport: Transport
    private let defaults: UserDefaults
    private let key = "kelvin.computers"
    private var sessions: [UUID: LabSession] = [:]

    init(transport: Transport = URLSessionTransport.shared, defaults: UserDefaults = .standard) {
        self.transport = transport
        self.defaults = defaults
        list = ComputerList.decode(defaults.data(forKey: key))
    }

    var current: Computer? { list.current }

    func session(for c: Computer) -> LabSession {
        if let s = sessions[c.id] { return s }
        let s = LabSession(computer: c, app: self)
        sessions[c.id] = s
        return s
    }

    func select(_ id: UUID) {
        list.selected = id
        save()
    }

    /// Offer the pairing code, keep the token, and learn the computer's name and other addresses.
    @discardableResult
    func pair(at base: URL, code: String) async throws -> Computer {
        let result = try await KelvinClient(base: base, token: nil, transport: transport).pair(code: code)
        var c = Computer(lab: base.host ?? "Kelvin", phoneName: result.name, role: result.role, addresses: [base])
        Keychain.save(result.token, for: c.id)
        if let me = try? await KelvinClient(base: base, token: result.token, transport: transport).me() {
            c.update(with: me, used: base)
        }
        if let old = list.add(c) {
            Keychain.delete(old)
            sessions[old]?.pause()
            sessions[old] = nil
        }
        save()
        Notifier.shared.ask()
        return c
    }

    /// What a session learned about its computer (its name, the address that worked).
    func update(_ c: Computer) {
        guard list.computers.contains(where: { $0.id == c.id && $0 != c }) else { return }
        list.update(c)
        save()
    }

    /// Forget a computer on this phone, and unpair the phone on the computer too if it answers.
    func remove(_ c: Computer) async {
        if let token = Keychain.token(for: c.id) {
            _ = try? await reach(c.addresses, token: token, transport: transport) { try await $0.forget() }
        }
        Keychain.delete(c.id)
        sessions[c.id]?.pause()
        sessions[c.id] = nil
        list.remove(c.id)
        save()
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

    private weak var app: AppModel?
    private let transport: Transport
    private var client: KelvinClient?
    private var loop: Task<Void, Never>?
    static let every: UInt64 = 3_000_000_000

    init(computer: Computer, app: AppModel) {
        self.computer = computer
        self.app = app
        transport = app.transport
    }

    var role: Role { computer.access }
    var route: Route? { connected.map(Route.init) }

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

    /// A client at an address that answers: the one that worked last, else each address in turn.
    func ready() async throws -> KelvinClient {
        if let client { return client }
        guard let token = Keychain.token(for: computer.id) else {
            throw KelvinError(.unpaired, "This phone is not paired with \(computer.lab) any more. Pair it again.")
        }
        let (c, me) = try await reach(computer.addresses, token: token, transport: transport) { try await $0.me() }
        client = c
        connected = c.base
        computer.update(with: me, used: c.base)
        app?.update(computer)
        return c
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

    func runs() async throws -> [RunSummary] { try await ready().runs() }

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
