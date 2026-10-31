import XCTest
@testable import KelvinKit

final class RouteTests: XCTestCase {
    func testRoutes() {
        func route(_ s: String) -> Route { Route(URL(string: s)!) }
        XCTAssertEqual(route("http://100.101.7.9:8771"), .lab)          // 100.64/10 alone: maybe a carrier's Wi-Fi
        let tail = URL(string: "http://100.101.7.9:8771")!
        XCTAssertEqual(Route(tail, kinds: ["http://100.101.7.9:8771": "tailscale"]), .tailscale)   // Kelvin says so
        XCTAssertEqual(Route(URL(string: "http://100.103.144.14:8771")!, kinds: ["http://100.103.144.14:8771": "network"]), .lab)
        XCTAssertEqual(route("http://lab-pc.tail1234.ts.net:8771"), .tailscale)
        XCTAssertEqual(route("http://192.168.1.50:8771"), .lab)
        XCTAssertEqual(route("http://10.0.4.2:8771"), .lab)
        XCTAssertEqual(route("http://172.20.1.1:8771"), .lab)
        XCTAssertEqual(route("http://lab-pc.local:8771"), .lab)
        XCTAssertEqual(route("http://172.40.1.1:8771"), .other)
        XCTAssertEqual(route("https://kelvin.example.org"), .other)
        XCTAssertTrue(Route.tailscale.encrypted)
        XCTAssertFalse(Route.lab.encrypted)
    }
}
