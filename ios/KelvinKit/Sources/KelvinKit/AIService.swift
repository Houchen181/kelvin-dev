import Foundation

/// The company a chat's messages go to: the one behind the model Kelvin answers with. Before a chat's first message
/// goes to a company, the app names it and waits for the person's OK (App Review guideline 5.1.2(i)).
public struct AIService: Equatable, Sendable {
    public var name: String      // Anthropic, OpenAI, Google...
    public var viaHub: Bool      // through the Kelvin hub, with the hub's keys (hubmodels.py)

    public init(name: String, viaHub: Bool = false) {
        self.name = name
        self.viaHub = viaHub
    }

    /// Kelvin's model connections (web/app.py make_providers, hubmodels.py, services.py), by the name before the
    /// colon in a model's key, and the company each one sends messages to.
    static let companies: [String: String] = [
        "anthropic": "Anthropic", "claudecode": "Anthropic",
        "openai": "OpenAI", "chatgpt": "OpenAI",
        "gemini": "Google",
        "opencode": "OpenCode", "opencode-go": "OpenCode",
        "openrouter": "OpenRouter", "deepseek": "DeepSeek", "groq": "Groq", "mistral": "Mistral AI", "xai": "xAI",
    ]

    /// Connections that send nothing to a company: a model server of the person's own, and the demo lab's.
    static let own: Set<String> = ["local", "demo"]

    /// Who answers with this model ("hub-anthropic:claude-opus-5-5"); nil when no company does. service: the label
    /// of the model's group in Kelvin's list, which names a service Kelvin doesn't know (one the person added).
    public static func of(model key: String, service: String? = nil) -> AIService? {
        let provider = key.split(separator: ":", maxSplits: 1).first.map(String.init) ?? ""
        if provider.isEmpty || own.contains(provider) { return nil }
        let hub = provider.hasPrefix("hub-")
        if let name = companies[hub ? String(provider.dropFirst(4)) : provider] {
            return AIService(name: name, viaHub: hub)
        }
        let named = (service ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return AIService(name: named.isEmpty ? provider : named, viaHub: hub)
    }

    /// What the app asks before a chat's first message goes to this company.
    public var question: String { "Send your chats to \(name)?" }

    /// Why: which model answers on which computer, and what goes to the company.
    public func explanation(model: String, computer: String) -> String {
        "Kelvin on \(computer) answers with \(model), from \(name)\(viaHub ? ", through the Kelvin hub" : ""). "
            + "What you write in chats, and what Kelvin reads from the lab to answer, goes to \(name). "
            + "You can take this back under Computers."
    }
}
