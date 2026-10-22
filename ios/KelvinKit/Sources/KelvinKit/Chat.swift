import Foundation

/// One event of Kelvin's answer as it streams (POST /api/chats/{id}/turn, one JSON object per line).
public enum ChatEvent: Equatable, Sendable {
    case started(model: String?, title: String?)
    case text(String)
    case reset
    case toolStart(id: String, name: String, input: JSONValue)
    case toolEnd(id: String, content: String, isError: Bool, runID: String?)
    case approval(ApprovalRequest)
    case approvalClosed(id: String)
    case runProgress(callID: String, done: Int, total: Int, status: String)
    case note(String)
    case memory(count: Int)
    case error(String)
    case ended(reason: String)
    case other(String)

    public init?(line: String) {
        guard let e = JSONValue.parse(line), let type = e["type"]?.string else { return nil }
        switch type {
        case "turn_start":
            self = .started(model: e["model"]?.string, title: e["title"]?.string)
        case "text":
            self = .text(e["delta"]?.string ?? "")
        case "reset":
            self = .reset
        case "tool_start":
            self = .toolStart(id: e["id"]?.string ?? "", name: e["name"]?.string ?? "", input: e["input"] ?? .null)
        case "tool_end":
            self = .toolEnd(id: e["id"]?.string ?? "", content: e["content"]?.text ?? "",
                            isError: e["is_error"]?.bool ?? false, runID: e["run_id"]?.string)
        case "approval":
            guard let id = e["id"]?.string else { return nil }
            self = .approval(ApprovalRequest(id: id, title: e["title"]?.string ?? "Kelvin asks",
                                             items: (e["items"]?.array ?? []).map(\.text),
                                             secondsLeft: e["seconds_left"]?.int))
        case "approval_closed":
            self = .approvalClosed(id: e["id"]?.string ?? "")
        case "run_progress":
            self = .runProgress(callID: e["call_id"]?.string ?? "", done: e["done"]?.int ?? 0,
                                total: e["total"]?.int ?? 0, status: e["status"]?.string ?? "")
        case "note":
            self = .note(e["text"]?.string ?? "")
        case "memory":
            self = .memory(count: e["notes"]?.array?.count ?? 0)
        case "error":
            self = .error(e["message"]?.string ?? "Something went wrong.")
        case "turn_end":
            self = .ended(reason: e["reason"]?.string ?? "end")
        default:
            self = .other(type)   // thinking, reviews, comparisons: the computer shows those
        }
    }
}

/// One line of a conversation as the phone shows it.
public struct ChatLine: Identifiable, Equatable, Sendable {
    public enum Kind: Equatable, Sendable { case user, assistant, step, approval, note, error }
    public enum Answer: Equatable, Sendable { case waiting, approved, declined, closed }

    public var id: String
    public var kind: Kind
    public var text: String
    public var detail: String?           // a step's result
    public var failed = false
    public var working = false           // a step still running, an answer still arriving
    public var approval: ApprovalRequest?
    public var answer: Answer?

    public init(id: String, kind: Kind, text: String, detail: String? = nil, failed: Bool = false,
                working: Bool = false) {
        self.id = id
        self.kind = kind
        self.text = text
        self.detail = detail
        self.failed = failed
        self.working = working
    }
}

/// A conversation: replayed from what Kelvin keeps, then grown by the events of the answer in progress.
public struct Transcript: Equatable, Sendable {
    public private(set) var lines: [ChatLine] = []
    public var title: String
    public var model: String?
    public var incognito: Bool
    public private(set) var answering = false
    private var serial = 0

    public init(title: String = "New chat", model: String? = nil, incognito: Bool = false) {
        self.title = title
        self.model = model
        self.incognito = incognito
    }

    public init(_ chat: ChatDetail) {
        self.init(title: chat.title, model: chat.model, incognito: chat.incognito)
        for m in chat.messages {
            switch m["role"]?.string {
            case "user":
                add(.user, m["text"]?.text ?? "")
            case "assistant":
                for b in m["blocks"]?.array ?? [] {
                    switch b["type"]?.string {
                    case "text":
                        let text = b["text"]?.text ?? ""
                        if !text.isEmpty { add(.assistant, text) }
                    case "tool_call":
                        let id = b["id"]?.string ?? next()
                        let label = Steps.label(b["name"]?.string ?? "", b["input"] ?? .null)
                        lines.append(ChatLine(id: "step-" + id, kind: .step, text: label))
                    default:
                        break
                    }
                }
            case "tool":
                for r in m["results"]?.array ?? [] {
                    finishStep(r["id"]?.string ?? "", content: r["content"]?.text ?? "", failed: r["is_error"]?.bool ?? false)
                }
            default:
                break
            }
        }
    }

    /// The person's message, shown at once.
    public mutating func say(_ text: String) {
        add(.user, text)
        answering = true
    }

    public mutating func apply(_ event: ChatEvent) {
        switch event {
        case .started(let model, let title):
            answering = true
            if let model { self.model = model }
            if let title, !title.isEmpty { self.title = title }
        case .text(let delta):
            if let i = lines.indices.last, lines[i].kind == .assistant, lines[i].working {
                lines[i].text += delta
            } else {
                var line = ChatLine(id: next(), kind: .assistant, text: delta)
                line.working = true
                lines.append(line)
            }
        case .reset:
            if let i = lines.indices.last, lines[i].kind == .assistant, lines[i].working { lines[i].text = "" }
        case .toolStart(let id, let name, let input):
            settleText()
            lines.append(ChatLine(id: "step-" + id, kind: .step, text: Steps.label(name, input), working: true))
        case .toolEnd(let id, let content, let isError, _):
            finishStep(id, content: content, failed: isError)
        case .approval(let request):
            guard !lines.contains(where: { $0.id == "ask-" + request.id }) else { return }
            var line = ChatLine(id: "ask-" + request.id, kind: .approval, text: request.title)
            line.approval = request
            line.answer = .waiting
            lines.append(line)
        case .approvalClosed(let id):
            if let i = lines.firstIndex(where: { $0.id == "ask-" + id }), lines[i].answer == .waiting {
                lines[i].answer = .closed
            }
        case .runProgress(let callID, let done, let total, let status):
            if let i = lines.firstIndex(where: { $0.id == "step-" + callID }) {
                lines[i].detail = status == "running" ? "\(done) of \(total) points" : "\(status): \(done) of \(total) points"
            }
        case .note(let text):
            add(.note, text)
        case .memory(let count):
            if count > 0 { add(.note, count == 1 ? "Kelvin recalled 1 note" : "Kelvin recalled \(count) notes") }
        case .error(let message):
            add(.error, message)
        case .ended(let reason):
            finish()
            if reason == "stopped" { add(.note, "Stopped.") }
        case .other:
            break
        }
    }

    /// The answer is over (or the stream broke): nothing is "working" any more.
    public mutating func finish() {
        answering = false
        for i in lines.indices where lines[i].working { lines[i].working = false }
    }

    /// The person answered a request from this chat.
    public mutating func answered(_ id: String, approve: Bool) {
        if let i = lines.firstIndex(where: { $0.id == "ask-" + id }) { lines[i].answer = approve ? .approved : .declined }
    }

    private mutating func settleText() {
        if let i = lines.indices.last, lines[i].kind == .assistant { lines[i].working = false }
    }

    private mutating func finishStep(_ id: String, content: String, failed: Bool) {
        guard let i = lines.firstIndex(where: { $0.id == "step-" + id }) else { return }
        lines[i].working = false
        lines[i].failed = failed || Steps.refused(content)
        lines[i].detail = Steps.summary(content)
    }

    private mutating func add(_ kind: ChatLine.Kind, _ text: String) {
        settleText()
        let id = next()
        lines.append(ChatLine(id: id, kind: kind, text: text))
    }

    private mutating func next() -> String {
        serial += 1
        return "line-\(serial)"
    }
}

/// The words for Kelvin's steps, as the portal says them (web/static/app.js stepLabel).
public enum Steps {
    public static func label(_ name: String, _ a: JSONValue) -> String {
        func s(_ key: String) -> String { a[key]?.text ?? "" }
        let plan = planName(s("plan"))
        switch name {
        case "lab_status": return "Checked the lab status"
        case "describe_instrument": return "Read the card of \(s("instrument"))"
        case "instrument_info": return "Read the folder of \(s("instrument"))"
        case "search_manual": return "Searched the \(s("instrument")) manual for “\(s("query"))”"
        case "show_sequence": return "Drew the \(s("protocol")) pulse sequence"
        case "read_examples": return "Read the maker's examples in \(s("path"))"
        case "try_card": return "Tried the card on \(s("instrument"))"
        case "get_value": return "Read \(s("instrument")).\(s("parameter"))"
        case "set_value": return "Set \(s("instrument")).\(s("parameter")) → \(s("value"))"
        case "send_raw": return "Sent \(s("command")) to \(s("instrument"))"
        case "undo_last": return "Undid the last change"
        case "safe_state": return "Sent every drive to safe"
        case "health_check": return "Ran health checks"
        case "validate_plan": return "Checked plan \(plan)"
        case "simulate_plan": return "Dry-ran plan \(plan)"
        case "run_plan": return "Ran plan \(plan)"
        case "list_runs": return "Listed recent runs"
        case "get_run_data": return "Read the data of run \(s("run_id"))"
        case "get_trace": return "Read a trace from run \(s("run_id"))"
        case "recent_events": return "Read the event log"
        case "wait": return "Waited \(s("seconds")) s"
        case "search_papers": return "Searched the literature for “\(s("query"))”"
        case "read_paper": return "Read \(s("paper"))"
        case "save_paper": return "Added \(s("paper")) to the reading list"
        case "make_report": return s("title").isEmpty ? "Wrote a report" : "Wrote a report: \(s("title"))"
        case "operate_computer": return "Operated the computer: \(s("task"))"
        case "remember":
            return (s("scope") == "global" ? "Remembered for every chat" : "Remembered for this project")
                + (s("text").isEmpty ? "" : ": \(s("text"))")
        default: return name.replacingOccurrences(of: "_", with: " ").capitalizedFirst
        }
    }

    /// A plan's name from its YAML ("plan: iv-sweep").
    static func planName(_ yaml: String) -> String {
        for line in yaml.split(separator: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("plan:") { return t.dropFirst(5).trimmingCharacters(in: .whitespaces) }
        }
        return ""
    }

    /// The first line that says what happened (not the dry-run preamble), at most 300 characters.
    public static func summary(_ content: String) -> String {
        let lines = content.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let line = lines.first { !$0.hasPrefix("dry run:") } ?? lines.first ?? ""
        return line.count > 300 ? String(line.prefix(299)) + "…" : line
    }

    /// Kelvin refused, was declined or tripped, though the tool itself worked.
    public static func refused(_ content: String) -> Bool {
        let head = content.split(separator: "\n").prefix(2).joined(separator: " ").lowercased()
        let starts = ["refused", "declined", "invalid plan", "not sent", "nothing to undo", "not run"]
        if starts.contains(where: { head.hasPrefix($0) }) { return true }
        let words = Set(head.split { !$0.isLetter }.map(String.init))
        return !words.isDisjoint(with: ["refused", "declined", "tripped", "aborted", "timeout", "failed", "error"])
    }
}

extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}
