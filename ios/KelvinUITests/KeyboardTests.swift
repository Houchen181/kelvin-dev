import XCTest

/// The keyboard always goes away again, so the tabs under it can be reached: a tap on the messages, or a swipe down.
final class KeyboardTests: XCTestCase {
    override func setUp() {
        continueAfterFailure = false
    }

    func launch(_ arguments: [String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-KelvinDemo", "YES"] + arguments
        app.launch()
        return app
    }

    /// Tap the message box: the on-screen keyboard comes up (skipped where a hardware keyboard stands in for it).
    func type(in app: XCUIApplication, _ id: String) throws -> XCUIElement {
        let field = app.descendants(matching: .any).matching(identifier: id).firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 15), "no message box")
        field.tap()
        guard app.keyboards.firstMatch.waitForExistence(timeout: 5) else {
            throw XCTSkip("no on-screen keyboard on this simulator (a hardware keyboard is connected)")
        }
        field.typeText("Taking the magnet")
        return field
    }

    /// Tap the messages where nothing else is: near their right edge, just above the message box and the keyboard.
    func tapBeside(_ messages: XCUIElement, above field: XCUIElement, in app: XCUIApplication) {
        let m = messages.frame
        let bottom = min(m.maxY, field.frame.minY - 12, app.keyboards.firstMatch.frame.minY)
        let point = CGVector(dx: m.maxX - 16, dy: bottom - 24)
        app.coordinate(withNormalizedOffset: .zero).withOffset(point).tap()
    }

    func gone(_ element: XCUIElement, _ app: XCUIApplication, what: String) -> Bool {
        let gone = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: element)
        if XCTWaiter().wait(for: [gone], timeout: 5) == .completed { return true }
        print("KEYBOARD-DEBUG after \(what):\n\(app.debugDescription)")   // in the CI's build log, to see why
        return false
    }

    func testTheKeyboardGoesAwayInTheTeamChat() throws {
        let app = launch(["-KelvinTab", "team"])
        let field = try type(in: app, "team-message")
        let messages = app.scrollViews["team-messages"]
        tapBeside(messages, above: field, in: app)
        XCTAssertTrue(gone(app.keyboards.firstMatch, app, what: "a tap"), "a tap on the messages left the keyboard up")

        field.tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))
        messages.swipeDown()
        XCTAssertTrue(gone(app.keyboards.firstMatch, app, what: "a swipe"), "a swipe down left the keyboard up")

        let lab = app.tabBars.buttons["Lab"]
        XCTAssertTrue(lab.isHittable, "the tabs can't be reached")
        lab.tap()
        XCTAssertTrue(app.navigationBars["Demo lab"].waitForExistence(timeout: 5))
    }

    func testTheKeyboardGoesAwayInAChat() throws {
        let app = launch(["-KelvinTab", "chat", "-KelvinOpen", "demo-chat"])
        let field = try type(in: app, "chat-message")
        tapBeside(app.scrollViews["chat-messages"], above: field, in: app)
        XCTAssertTrue(gone(app.keyboards.firstMatch, app, what: "a tap"), "a tap on the conversation left the keyboard up")
        XCTAssertTrue(app.tabBars.buttons["Team"].isHittable, "the tabs can't be reached")
    }
}
