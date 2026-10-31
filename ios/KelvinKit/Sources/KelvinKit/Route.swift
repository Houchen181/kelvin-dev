import Foundation

/// How the phone reaches a computer at an address, in words for the screen.
public enum Route: String, Equatable, Sendable {
    case tailscale = "Tailscale"
    case lab = "Lab network"
    case other = "Internet"
    case hub = "Kelvin hub"

    /// `kinds`: what Kelvin said each address is (url -> tailscale, network or public), which beats guessing:
    /// 100.64.0.0/10 is Tailscale's, but some Wi-Fi networks (Xfinity's) hand out those addresses too.
    public init(_ url: URL, kinds: [String: String]? = nil) {
        switch kinds?[url.absoluteString] {
        case "tailscale"?: self = .tailscale; return
        case "network"?: self = .lab; return
        case "public"?: self = .other; return
        case "hub"?: self = .hub; return
        default: break
        }
        let host = (url.host ?? "").lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if host.hasSuffix(".ts.net") {
            self = .tailscale
        } else if host.hasSuffix(".local") || !host.contains(".") && !host.contains(":") {
            self = .lab
        } else if let ip = ipv4(host) {
            if ip.0 == 100 && (64...127).contains(ip.1) {
                self = .lab              // 100.64.0.0/10 without Kelvin's word: maybe Tailscale, so claim no encryption
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

    /// Encrypted on the way: Tailscale is, and the Kelvin hub's https both ways (the hub itself sees what passes);
    /// a plain lab network or the internet over http is not.
    public var encrypted: Bool { self == .tailscale || self == .hub }
}

private func ipv4(_ host: String) -> (Int, Int, Int, Int)? {
    let parts = host.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
    guard parts.count == 4, parts.allSatisfy({ ($0 ?? -1) >= 0 && ($0 ?? 256) <= 255 }) else { return nil }
    return (parts[0]!, parts[1]!, parts[2]!, parts[3]!)
}
