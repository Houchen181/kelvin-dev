import XCTest

/// At the largest text sizes the chat's message bar still fits together: the mic grows with its glyph, beside the
/// message box (a fixed frame once let the glyph spill over the box).
final class TextSizeTests: XCTestCase {
    override func setUp() {
        continueAfterFailure = false
    }

    func testTheMicGrowsWithTheTextBesideTheMessageBox() {
        let app = XCUIApplication()
        app.launchArguments = ["-KelvinFresh", "YES", "-KelvinDemo", "YES", "-KelvinTab", "chat",
                               "-KelvinOpen", "demo-chat",
                               "-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        app.launch()
        let field = app.descendants(matching: .any).matching(identifier: "chat-message").firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 15), "no message box")
        let mic = app.buttons["Talk"]
        XCTAssertTrue(mic.waitForExistence(timeout: 5), "no mic")
        XCTAssertGreaterThan(mic.frame.height, 34)                       // as large as its glyph, not a fixed 34 points
        XCTAssertLessThanOrEqual(mic.frame.maxX, field.frame.minX)
    }
}
