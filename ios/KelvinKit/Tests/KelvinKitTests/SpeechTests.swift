import XCTest
@testable import KelvinKit

final class SpeechTests: XCTestCase {
    func testWhatWasHeardFollowsWhatWasTyped() {
        XCTAssertEqual(Voice.merge("", "Sweep the gate to 2 volts."), "Sweep the gate to 2 volts.")
        XCTAssertEqual(Voice.merge("Then", "sweep the gate"), "Then sweep the gate")
        XCTAssertEqual(Voice.merge("Then ", "sweep the gate"), "Then sweep the gate")
        XCTAssertEqual(Voice.merge("Then", "  "), "Then")
    }
}
