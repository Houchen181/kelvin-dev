import Foundation
import KelvinKit
import Security
import UserNotifications

/// Requests over URLSession. Two sessions: quick calls give up after 20 s of silence, so the next address gets its
/// turn; a chat answer may be silent for a long time while Kelvin waits for someone's approval.
struct URLSessionTransport: Transport {
    static let shared = URLSessionTransport()

    private let quick: URLSession
    private let streaming: URLSession

    init() {
        func session(_ idle: TimeInterval) -> URLSession {
            let c = URLSessionConfiguration.ephemeral
            c.timeoutIntervalForRequest = idle
            c.waitsForConnectivity = false
            c.requestCachePolicy = .reloadIgnoringLocalCacheData
            c.httpShouldSetCookies = false
            return URLSession(configuration: c)
        }
        quick = session(20)
        streaming = session(3600)
    }

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await quick.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        return (data, http)
    }

    func lines(for request: URLRequest) async throws -> (HTTPURLResponse, AsyncThrowingStream<String, Error>) {
        let (bytes, response) = try await streaming.bytes(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        let stream = AsyncThrowingStream<String, Error> { continuation in
            let task = Task {
                do {
                    for try await line in bytes.lines { continuation.yield(line) }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
        return (http, stream)
    }
}

/// Each computer's token, in the Keychain: on this phone only, readable once it has been unlocked after a restart.
enum Keychain {
    static let service = "Kelvin pairing"

    private static func query(_ id: UUID) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: id.uuidString]
    }

    static func save(_ token: String, for id: UUID) {
        SecItemDelete(query(id) as CFDictionary)
        var add = query(id)
        add[kSecValueData as String] = Data(token.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }

    static func token(for id: UUID) -> String? {
        var q = query(id)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func delete(_ id: UUID) {
        SecItemDelete(query(id) as CFDictionary)
    }
}

/// Tells the person about requests and alarms that appear while the app is open (also on another tab or screen).
/// What was already there when the app first looked is on screen anyway, so it makes no sound.
@MainActor
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    static let shared = Notifier()

    private var seen = Set<String>()
    private var looked = Set<UUID>()

    func ask() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    /// Returns the titles it posted.
    @discardableResult
    func look(at state: LabState, on computer: Computer) -> [String] {
        let asks = state.pending.map { ("ask:\(computer.id):\($0.id)", "Kelvin asks: \($0.title)", $0.items.joined(separator: "\n")) }
        let alarms = state.openAlarms.map { ("alarm:\(computer.id):\($0.id)", "Alarm in \(state.lab)", $0.text) }
        let first = !looked.contains(computer.id)
        looked.insert(computer.id)
        var posted: [String] = []
        for (key, title, body) in asks + alarms where !seen.contains(key) {
            seen.insert(key)
            if first { continue }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            content.sound = .default
            content.threadIdentifier = computer.id.uuidString
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: key, content: content, trigger: nil))
            posted.append(title)
        }
        return posted
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            willPresent notification: UNNotification) async
        -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }
}
