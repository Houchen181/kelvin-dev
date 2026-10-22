import Foundation

/// Where a Kelvin computer is, and (from its QR code) the one-time pairing code.
public struct PairingLink: Equatable, Sendable {
    public var base: URL
    public var code: String?

    public init(base: URL, code: String?) {
        self.base = base
        self.code = code
    }
}

public enum Pairing {
    /// The port of Kelvin's phone listener unless its Settings say otherwise.
    public static let defaultPort = 8771

    /// Read a pairing QR code. Kelvin's holds http://<address>:<port>/pair#<6-digit code>; a link to the phone page
    /// (/m) or to the bare address works too, without a code.
    public static func parse(_ text: String) -> PairingLink? {
        guard let c = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = c.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = c.host, !host.isEmpty,
              ["", "/", "/pair", "/m"].contains(c.path) else { return nil }
        var b = URLComponents()
        b.scheme = scheme
        b.percentEncodedHost = c.percentEncodedHost
        b.port = c.port ?? (scheme == "http" ? defaultPort : nil)
        guard let base = b.url else { return nil }
        return PairingLink(base: base, code: c.fragment.map(digits).flatMap { isCode($0) ? $0 : nil })
    }

    /// An address a person typed: "192.168.1.20", "192.168.1.20:8771", "lab-pc.tail1234.ts.net", or a whole link.
    public static func address(_ typed: String) -> URL? {
        let t = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, !t.contains(" ") else { return nil }
        let lower = t.lowercased()
        let link = lower.hasPrefix("http://") || lower.hasPrefix("https://") ? t : "http://" + t
        return parse(link)?.base
    }

    /// A code as people type it: "123 456" and "123-456" are "123456".
    public static func digits(_ typed: String) -> String {
        String(typed.filter { !$0.isWhitespace && $0 != "-" })
    }

    public static func isCode(_ s: String) -> Bool {
        s.count == 6 && s.allSatisfy { $0.isASCII && $0.isNumber }
    }
}

/// How the phone reaches a computer at an address, in words for the screen.
public enum Route: String, Equatable, Sendable {
    case tailscale = "Tailscale"
    case lab = "Lab network"
    case other = "Internet"

    public init(_ url: URL) {
        let host = (url.host ?? "").lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if host.hasSuffix(".ts.net") {
            self = .tailscale
        } else if host.hasSuffix(".local") || !host.contains(".") && !host.contains(":") {
            self = .lab
        } else if let ip = ipv4(host) {
            if ip.0 == 100 && (64...127).contains(ip.1) {
                self = .tailscale                                   // 100.64.0.0/10
            } else if ip.0 == 10 || ip.0 == 172 && (16...31).contains(ip.1) || ip.0 == 192 && ip.1 == 168 {
                self = .lab
            } else {
                self = .other
            }
        } else if host.hasPrefix("fd7a:115c:a1e0:") {
            self = .tailscale                                       // Tailscale's IPv6 range
        } else if host.hasPrefix("fd") || host.hasPrefix("fe80:") {
            self = .lab
        } else {
            self = .other
        }
    }

    /// Encrypted on the way: Tailscale is; a plain lab network or the internet over http is not.
    public var encrypted: Bool { self == .tailscale }
}

private func ipv4(_ host: String) -> (Int, Int, Int, Int)? {
    let parts = host.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
    guard parts.count == 4, parts.allSatisfy({ ($0 ?? -1) >= 0 && ($0 ?? 256) <= 255 }) else { return nil }
    return (parts[0]!, parts[1]!, parts[2]!, parts[3]!)
}
