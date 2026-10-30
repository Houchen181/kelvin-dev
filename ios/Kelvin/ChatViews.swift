import KelvinKit
import SwiftUI

/// The conversations on this computer, and new ones.
struct ChatListView: View {
    @ObservedObject var session: LabSession
    @State var chats: [ChatSummary] = []
    @State var failure: String?
    @State var opened: ChatSummary?

    var body: some View {
        List {
            Section {
                NavigationLink {
                    ChatView(session: session, chat: nil, incognito: false)
                } label: {
                    Label("New chat", systemImage: "square.and.pencil")
                }
                NavigationLink {
                    ChatView(session: session, chat: nil, incognito: true)
                } label: {
                    Label("New incognito chat", systemImage: "eye.slash")
                }
            } footer: {
                Text("An incognito chat uses no memory and saves none.")
            }
            Section("Recent") {
                if chats.isEmpty { Text("No chats yet.").foregroundStyle(.secondary) }
                ForEach(chats) { c in
                    NavigationLink {
                        ChatView(session: session, chat: c, incognito: c.incognito)
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                if c.incognito { Image(systemName: "eye.slash").foregroundStyle(.secondary) }
                                Text(c.title).lineLimit(1)
                                if session.state?.active?.chatID == c.id {
                                    Spacer()
                                    ProgressView()
                                }
                            }
                            Text(Date(timeIntervalSince1970: c.updated), style: .relative)
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .navigationTitle("Chat")
        .navigationDestination(isPresented: Binding(get: { opened != nil }, set: { if !$0 { opened = nil } })) {
            if let c = opened { ChatView(session: session, chat: c, incognito: c.incognito) }
        }
        .task { await load() }
        .refreshable { await load() }
        .failure($failure)
    }

    @MainActor
    func load() async {
        do {
            chats = try await session.chats()
            if let c = chats.first(where: { Launch.open($0.id) }) { opened = c }
        } catch {
            failure = error.localizedDescription
        }
    }
}

struct ChatView: View {
    @StateObject var model: ChatModel
    @StateObject var voice = Dictation()
    @FocusState private var typing: Bool

    init(session: LabSession, chat: ChatSummary?, incognito: Bool) {
        _model = StateObject(wrappedValue: ChatModel(session: session, chat: chat, incognito: incognito))
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if model.transcript.incognito {
                        Label("Incognito: Kelvin uses no memory here and keeps none of it.", systemImage: "eye.slash")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                    ForEach(model.transcript.lines) { line in
                        LineView(line: line, model: model)
                            .id(line.id)
                    }
                    if model.transcript.answering {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("Kelvin is working…").font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                    if let p = model.problem {
                        Text(p).font(.footnote).foregroundStyle(.red)
                    }
                    Color.clear.frame(height: 1).id("end")
                }
                .padding()
            }
            .scrollDismissesKeyboard(.immediately)   // a swipe on the conversation puts the keyboard away (the tabs are under it)
            .onTapGesture { typing = false }         // and so does a tap on it
            .onChange(of: model.transcript.lines.count) { _ in
                withAnimation { proxy.scrollTo("end", anchor: .bottom) }
            }
            .onChange(of: model.transcript.lines.last?.text) { _ in
                proxy.scrollTo("end", anchor: .bottom)
            }
        }
        .safeAreaInset(edge: .bottom) { Composer(model: model, voice: voice, typing: $typing) }
        .onDisappear { voice.cancel() }
        .navigationTitle(model.transcript.title)
        .navigationBarTitleDisplayMode(.inline)
        .task { await model.load() }
        .refreshable { await model.load() }
    }
}

struct Composer: View {
    @ObservedObject var model: ChatModel
    @ObservedObject var voice: Dictation
    var typing: FocusState<Bool>.Binding

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let p = voice.problem {
                Text(p).font(.caption).foregroundStyle(.red)
            }
            HStack(alignment: .bottom, spacing: 8) {
                fields
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }

    @ViewBuilder
    private var fields: some View {
            Button {
                voice.toggle(typed: model.draft) { model.draft = $0 }
            } label: {
                Image(systemName: voice.listening ? "mic.fill" : "mic")
                    .font(.title2)
                    .foregroundStyle(voice.listening ? Color.red : Color.accentColor)
                    .frame(width: 32, height: 34)
            }
            .disabled(model.transcript.answering)
            .accessibilityLabel(voice.listening ? "Stop listening" : "Talk")
            TextField(voice.listening ? "Listening…" : "Message Kelvin", text: $model.draft, axis: .vertical)
                .lineLimit(1...6)
                .textFieldStyle(.roundedBorder)
                .focused(typing)
                .accessibilityIdentifier("chat-message")
            if model.transcript.answering {
                Button {
                    Task { await model.stop() }
                } label: {
                    Image(systemName: "stop.circle.fill").font(.title)
                }
                .tint(.red)
                .accessibilityLabel("Stop")
            } else {
                Button {
                    voice.cancel()
                    model.send()
                } label: {
                    Image(systemName: "arrow.up.circle.fill").font(.title)
                }
                .disabled(model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityLabel("Send")
            }
    }
}

struct LineView: View {
    let line: ChatLine
    @ObservedObject var model: ChatModel
    @State var open = false

    var body: some View {
        switch line.kind {
        case .user:
            HStack {
                Spacer(minLength: 40)
                Text(line.text)
                    .padding(10)
                    .background(Color.accentColor.opacity(0.15), in: RoundedRectangle(cornerRadius: 14))
                    .textSelection(.enabled)
            }
        case .assistant:
            VStack(alignment: .leading, spacing: 4) {
                Text(markdown(open ? line.full ?? line.text : line.text))
                    .textSelection(.enabled)
                if line.full != nil {   // Kelvin condensed a long answer: the whole one is a tap away
                    Button(open ? "Show the short answer"
                                : "Full answer" + (line.fullWords.map { " (\($0) words)" } ?? "")) { open.toggle() }
                        .font(.caption)
                }
            }
        case .step:
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    if line.working {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: line.failed ? "exclamationmark.triangle.fill" : "checkmark.circle")
                            .foregroundStyle(line.failed ? Color.orange : Color.green)
                    }
                    Text(line.text).font(.subheadline)
                }
                if let detail = line.detail, !detail.isEmpty {
                    Text(detail)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(open ? nil : 2)
                        .onTapGesture { open.toggle() }
                }
            }
        case .approval:
            if let r = line.approval {
                ChatApproval(request: r, answer: line.answer ?? .closed, model: model)
            }
        case .note:
            Text(line.text).font(.footnote).italic().foregroundStyle(.secondary)
        case .error:
            Label(line.text, systemImage: "xmark.octagon").font(.footnote).foregroundStyle(.red)
        }
    }

    private func markdown(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}

/// A request Kelvin made during this answer, to approve or decline right here.
struct ChatApproval: View {
    let request: ApprovalRequest
    let answer: ChatLine.Answer
    @ObservedObject var model: ChatModel
    @State var confirming = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(request.title).font(.headline)
            ForEach(Array(request.items.enumerated()), id: \.offset) { _, item in
                Text(item).font(.subheadline.monospacedDigit())
            }
            switch answer {
            case .waiting:
                HStack {
                    if model.session.role.canApprove {
                        Button("Approve") { confirming = true }.buttonStyle(.borderedProminent)
                    }
                    Button("Decline") { Task { await model.answer(request, approve: false) } }.buttonStyle(.bordered)
                }
            case .approved:
                Label("Approved", systemImage: "checkmark").foregroundStyle(.green)
            case .declined:
                Label("Declined", systemImage: "xmark").foregroundStyle(.secondary)
            case .closed:
                Text("Answered").foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
        .confirmationDialog("Approve: \(request.title)?", isPresented: $confirming, titleVisibility: .visible) {
            Button("Approve") { Task { await model.answer(request, approve: true) } }
        } message: {
            Text(request.items.joined(separator: "\n"))
        }
    }
}
