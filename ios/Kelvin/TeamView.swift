import KelvinKit
import SwiftUI

/// The lab's team chat on the phone, as it is on the Kelvin computer: everyone there reads the same messages. Each
/// person writes under the name set on their phone, also when people share one Kelvin account.
@MainActor
final class TeamModel: ObservableObject {
    @Published private(set) var thread = TeamThread()
    @Published private(set) var loaded = false
    @Published private(set) var problem: String?
    @Published private(set) var sending = false
    @Published var draft = ""

    let session: LabSession
    private var loading = false
    private var again = false

    init(session: LabSession) {
        self.session = session
    }

    /// What is new since the messages here (the latest messages, the first time).
    func load() async {
        if loading {
            again = true
            return
        }
        loading = true
        repeat {
            again = false
            do {
                let page = try await session.team(after: thread.through)
                if !thread.take(page) {   // the team chat began again: what is shown now is what is read
                    let fresh = try await session.team(after: 0)
                    thread.take(fresh)
                    session.readTeam(upTo: thread.through, force: true)
                }
                loaded = true
                problem = nil
            } catch {
                problem = error.localizedDescription
            }
        } while again
        loading = false
    }

    /// The messages before the first one here.
    func older() async {
        guard !loading, thread.more, let first = thread.messages.first?.id else { return }
        loading = true
        do {
            let page = try await session.team(before: first)
            thread.take(page.messages, older: true)
        } catch {
            problem = error.localizedDescription
        }
        loading = false
        if again { await load() }   // something new came meanwhile
    }

    /// Send the draft under `name`, then fetch what others wrote meanwhile. True when it was sent.
    @discardableResult
    func send(as name: String) async -> Bool {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return false }
        sending = true
        defer { sending = false }
        do {
            let m = try await session.postTeam(name: name, text: text)
            draft = ""
            thread.sent(m)
            problem = nil
        } catch {
            problem = error.localizedDescription
            return false
        }
        await load()
        return true
    }
}

struct TeamView: View {
    @ObservedObject var session: LabSession
    let active: Bool                   // its tab is the one in front
    @EnvironmentObject var app: AppModel
    @StateObject var model: TeamModel
    @State var naming = false
    @State var name = ""
    @State var sendAfterNaming = false
    @FocusState private var typing: Bool

    init(session: LabSession, active: Bool) {
        _session = ObservedObject(wrappedValue: session)
        self.active = active
        _model = StateObject(wrappedValue: TeamModel(session: session))
    }

    var body: some View {
        Group {
            if session.computer.hasTeamAndAnalysis {
                chat
            } else {
                Text("The team chat needs Kelvin 0.3.32 or later on the lab computer.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                    .padding(32)
            }
        }
        .navigationTitle("Team")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                if session.role.canWriteTeam && session.computer.hasTeamAndAnalysis {
                    Button { askName(thenSend: false) } label: {
                        Label(app.teamName.isEmpty ? "Your name" : app.teamName, systemImage: "person.crop.circle")
                            .labelStyle(.titleAndIcon)
                    }
                }
            }
        }
        .alert("Your name in the team chat", isPresented: $naming) {
            TextField("Name", text: $name)
                .textInputAutocapitalization(.words)
            Button("Save") { saveName() }
            Button("Cancel", role: .cancel) { sendAfterNaming = false }
        } message: {
            Text("Each person writes under their own name.")
        }
    }

    private var chat: some View {
        GeometryReader { box in
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 4) {
                        if model.thread.more {
                            Button("Earlier messages") { Task { await model.older() } }
                                .font(.footnote)
                        }
                        if model.loaded && model.thread.messages.isEmpty {
                            Text("No messages yet").foregroundStyle(.secondary).padding(.top, 40)
                        }
                        ForEach(model.thread.lines()) { line in
                            if let day = line.day {
                                Text(TeamView.day(day))
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.secondary)
                                    .padding(.top, 10)
                            }
                            TeamBubble(line: line, mine: !app.teamName.isEmpty && line.message.name == app.teamName)
                                .id(line.id)
                        }
                        if let p = model.problem {
                            Text(p).font(.footnote).foregroundStyle(.red)
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding()
                    .frame(maxWidth: .infinity, minHeight: box.size.height, alignment: .top)   // the empty space below too
                    .contentShape(Rectangle())
                    .onTapGesture {   // a tap on the messages puts the keyboard away (the tabs are under it)
                        typing = false
                        Keyboard.dismiss()
                    }
                }
                .scrollDismissesKeyboard(.immediately)   // and so does a swipe on them
                .accessibilityIdentifier("team-messages")
                .onChange(of: model.thread.last) { _ in
                    withAnimation { proxy.scrollTo("end", anchor: .bottom) }
                }
                .onChange(of: model.thread.through) { _ in markRead() }   // whatever load brought it on screen
            }
        }
        .safeAreaInset(edge: .bottom) { composer }
        .refreshable { await model.load() }
        .task(id: active) {
            guard active else { return }
            await model.load()
            markRead()
        }
        .onChange(of: session.state?.team) { _ in
            guard active else { return }
            Task {
                await model.load()
                markRead()
            }
        }
    }

    private var composer: some View {
        Group {
            if session.role.canWriteTeam {
                HStack(alignment: .bottom, spacing: 8) {
                    TextField("Message the team", text: $model.draft, axis: .vertical)
                        .lineLimit(1...6)
                        .textFieldStyle(.roundedBorder)
                        .focused($typing)
                        .accessibilityIdentifier("team-message")
                    Button { send() } label: {
                        Image(systemName: "arrow.up.circle.fill").font(.title)
                    }
                    .disabled(model.sending || model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("Send")
                }
            } else {
                Text("This phone can read; write on the Kelvin computer.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private func markRead() {   // every message up to `through` is on screen; one just sent beyond it waits for it
        if active && model.thread.through > 0 { session.readTeam(upTo: model.thread.through) }
    }

    private func askName(thenSend: Bool) {
        name = app.teamName.isEmpty ? (app.account?.name ?? "") : app.teamName
        sendAfterNaming = thenSend
        naming = true
    }

    private func saveName() {
        let n = name.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard !n.isEmpty else {
            sendAfterNaming = false
            return
        }
        app.teamName = String(n.prefix(40))
        if sendAfterNaming {
            sendAfterNaming = false
            send()
        }
    }

    private func send() {
        guard !app.teamName.isEmpty else { return askName(thenSend: true) }
        let name = app.teamName
        Task {
            if await model.send(as: name) { markRead() }
        }
    }

    /// Today, Yesterday, or the date.
    static func day(_ d: Date) -> String {
        let c = Calendar.current
        if c.isDateInToday(d) { return "Today" }
        if c.isDateInYesterday(d) { return "Yesterday" }
        let thisYear = c.component(.year, from: d) == c.component(.year, from: Date())
        let style = Date.FormatStyle.dateTime.weekday(.abbreviated).month(.abbreviated).day()
        return d.formatted(thisYear ? style : style.year())
    }
}

/// One message: the writer's name and time unless it follows their own, then the text; this phone's own on the right.
struct TeamBubble: View {
    let line: TeamThread.Line
    let mine: Bool

    var body: some View {
        VStack(alignment: mine ? .trailing : .leading, spacing: 2) {
            if !line.continued {
                HStack(spacing: 6) {
                    Text(line.message.name).fontWeight(.semibold)
                    if let d = line.message.date { Text(d, style: .time) }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .padding(.top, 6)
            }
            Text(line.message.text)
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(mine ? Color.accentColor.opacity(0.15) : Color.secondary.opacity(0.12),
                            in: RoundedRectangle(cornerRadius: 14))
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: mine ? .trailing : .leading)
        .padding(mine ? .leading : .trailing, 40)
        .accessibilityElement(children: .combine)
    }
}
