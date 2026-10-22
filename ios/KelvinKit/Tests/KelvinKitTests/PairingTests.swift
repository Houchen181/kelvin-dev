import XCTest
@testable import KelvinKit

final class PairingTests: XCTestCase {
    func testTheQRCodeKelvinShows() {
        let link = Pairing.parse("http://192.168.1.50:8771/pair#042137")
        XCTAssertEqual(link?.base.absoluteString, "http://192.168.1.50:8771")
        XCTAssertEqual(link?.code, "042137")
        XCTAssertEqual(Pairing.parse("  http://100.101.7.9:9000/pair#123456\n")?.base.absoluteString, "http://100.101.7.9:9000")
        XCTAssertEqual(Pairing.parse("HTTP://lab-pc.tail1234.ts.net:8771/m")?.base.absoluteString,
                       "http://lab-pc.tail1234.ts.net:8771")
        XCTAssertNil(Pairing.parse("http://192.168.1.50:8771/m")?.code)
        XCTAssertNil(Pairing.parse("http://192.168.1.50:8771/pair#12345")?.code)       // five digits is no code
    }

    func testNotAKelvinCode() {
        XCTAssertNil(Pairing.parse("WIFI:S:lab;T:WPA;P:secret;;"))
        XCTAssertNil(Pairing.parse("https://example.com/some/page"))
        XCTAssertNil(Pairing.parse("ftp://192.168.1.50/pair#123456"))
        XCTAssertNil(Pairing.parse(""))
    }

    func testTypedAddresses() {
        XCTAssertEqual(Pairing.address("192.168.1.50")?.absoluteString, "http://192.168.1.50:8771")
        XCTAssertEqual(Pairing.address(" 192.168.1.50:9000 ")?.absoluteString, "http://192.168.1.50:9000")
        XCTAssertEqual(Pairing.address("lab-pc.tail1234.ts.net")?.absoluteString, "http://lab-pc.tail1234.ts.net:8771")
        XCTAssertEqual(Pairing.address("http://192.168.1.50:8771/m")?.absoluteString, "http://192.168.1.50:8771")
        XCTAssertEqual(Pairing.address("https://kelvin.example.org")?.absoluteString, "https://kelvin.example.org")
        XCTAssertNil(Pairing.address("lab pc"))
        XCTAssertNil(Pairing.address(""))
    }

    func testCodesAsPeopleTypeThem() {
        XCTAssertEqual(Pairing.digits("123 456"), "123456")
        XCTAssertEqual(Pairing.digits("123-456"), "123456")
        XCTAssertTrue(Pairing.isCode("000123"))
        XCTAssertFalse(Pairing.isCode("12345a"))
        XCTAssertFalse(Pairing.isCode("１２３４５６"))   // full-width digits are not what Kelvin sent
    }

    func testRoutes() {
        func route(_ s: String) -> Route { Route(URL(string: s)!) }
        XCTAssertEqual(route("http://100.101.7.9:8771"), .tailscale)
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
