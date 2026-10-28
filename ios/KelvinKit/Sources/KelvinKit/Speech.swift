import Foundation

/// Voice input: where the heard text goes in the message box.
public enum Voice {
    /// The message box with what was heard after what was already typed.
    public static func merge(_ typed: String, _ heard: String) -> String {
        let h = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        if h.isEmpty { return typed }
        if typed.isEmpty { return h }
        return typed.last?.isWhitespace == true ? typed + h : typed + " " + h
    }
}
