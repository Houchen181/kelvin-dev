import XCTest

/// The app walked through as a person would, on the demo lab: every screen reached, typed in, left again. A step that
/// can't be done fails with the screen's elements in the log, to see why.
final class WalkthroughTests: XCTestCase {
    var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
    }

    func launch(_ arguments: [String]) {
        app = XCUIApplication()
        app.launchArguments = ["-KelvinFresh", "YES"] + arguments
        app.launch()
    }

    /// Wait for `element`; if it never comes, fail saying what was being done.
    @discardableResult
    func must(_ element: XCUIElement, _ doing: String, timeout: TimeInterval = 10, file: StaticString = #filePath,
              line: UInt = #line) -> XCUIElement {
        if !element.waitForExistence(timeout: timeout) {
            print("WALKTHROUGH-DEBUG while \(doing):\n\(app.debugDescription)")
            XCTFail("can't \(doing)", file: file, line: line)
        }
        return element
    }

    func gone(_ element: XCUIElement, _ doing: String, file: StaticString = #filePath, line: UInt = #line) {
        let away = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == false"), object: element)
        if XCTWaiter().wait(for: [away], timeout: 10) != .completed {
            print("WALKTHROUGH-DEBUG while \(doing):\n\(app.debugDescription)")
            XCTFail("\(doing): it is still there", file: file, line: line)
        }
    }

    /// A confirmation's button: in an action sheet, an alert, or (newer iOS) a popover.
    func confirm(_ label: String, _ doing: String) {
        for query in [app.sheets.buttons, app.alerts.buttons, app.popovers.buttons] where query[label].exists {
            query[label].tap()
            return
        }
        let all = app.buttons.matching(identifier: label).allElementsBoundByIndex
        guard let last = all.last else { return XCTFail("no \(label) to \(doing)") }
        last.tap()
    }

    func text(containing s: String) -> XCUIElement {
        app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", s)).firstMatch
    }

    /// The one of the buttons so named that is on screen (another tab can have one too).
    func tap(_ label: String, _ doing: String) {
        let shown = app.buttons.matching(identifier: label).allElementsBoundByIndex.first { $0.isHittable }
        guard let shown else {
            print("WALKTHROUGH-DEBUG while \(doing):\n\(app.debugDescription)")
            return XCTFail("can't \(doing)")
        }
        shown.tap()
    }

    /// Wholly on screen: below the navigation bar and above the tabs.
    func onScreen(_ element: XCUIElement) -> Bool {
        guard element.exists else { return false }
        let f = element.frame
        let top = app.navigationBars.firstMatch.exists ? app.navigationBars.firstMatch.frame.maxY : 0
        let bottom = app.tabBars.firstMatch.exists ? app.tabBars.firstMatch.frame.minY : app.frame.maxY
        return f.minY >= top && f.maxY <= bottom
    }

    /// Scroll the list until `element` is on screen, looking further down first (or up). A list holds only the rows
    /// near the screen, so one far off isn't there until the list gets near it.
    @discardableResult
    func reach(_ element: XCUIElement, _ doing: String, down: Bool = true, file: StaticString = #filePath,
               line: UInt = #line) -> XCUIElement {
        let list = app.collectionViews.firstMatch
        var moves = 0
        while !onScreen(element) && moves < 16 {
            let further = (moves < 8) == down
            // dragged by the margin beside the rows, so no row's control takes the touch
            let from = list.coordinate(withNormalizedOffset: CGVector(dx: 0.98, dy: further ? 0.65 : 0.35))
            let to = list.coordinate(withNormalizedOffset: CGVector(dx: 0.98, dy: further ? 0.35 : 0.65))
            from.press(forDuration: 0.05, thenDragTo: to, withVelocity: .slow, thenHoldForDuration: 0.2)
            moves += 1
        }
        if !onScreen(element) {
            print("WALKTHROUGH-DEBUG while \(doing):\n\(app.debugDescription)")
            XCTFail("can't \(doing)", file: file, line: line)
        }
        return element
    }

    /// Tap the list's row with something labelled `label` (or starting so) at `x` across it: a menu in a row opens
    /// only from its own part of the row.
    func tapRow(_ label: String, at x: CGFloat, _ doing: String, file: StaticString = #filePath, line: UInt = #line) {
        let row = app.cells.containing(NSPredicate(format: "label BEGINSWITH %@", label)).firstMatch
        reach(row, doing, file: file, line: line).coordinate(withNormalizedOffset: CGVector(dx: x, dy: 0.5)).tap()
    }

    func testTheWelcomeScreensSheetsCanBeFilledInAndLeft() {
        launch([])
        must(app.buttons["Pair with a code"], "see the welcome screen").tap()
        must(app.navigationBars["Pair with Kelvin"], "open pairing")
        must(app.buttons["Type it"], "choose to type the code").tap()   // it opens on the camera
        must(app.textFields["Address, e.g. 192.168.1.20"], "find the address field").tap()
        app.typeText("192.168.1.20")
        must(app.textFields["6-digit code"], "find the code field").tap()
        app.typeText("123456")
        XCTAssertTrue(must(app.buttons["Pair"], "find Pair").isHittable, "Pair is under the keyboard")
        app.navigationBars.buttons["Cancel"].tap()
        gone(app.navigationBars["Pair with Kelvin"], "leave pairing")

        must(app.buttons["Sign in with your Kelvin account"], "see the welcome screen again").tap()
        must(app.navigationBars["Kelvin account"], "open the account sheet")
        must(app.textFields["Email"], "find the email field").tap()
        app.typeText("ada@example.org")
        must(app.secureTextFields["Password"], "find the password field").tap()
        app.typeText("not-a-real-password")
        XCTAssertTrue(must(app.buttons["Sign in"], "find Sign in").isHittable, "Sign in is under the keyboard")
        app.navigationBars.buttons["Cancel"].tap()
        gone(app.navigationBars["Kelvin account"], "leave the account sheet")
        let notNow = app.buttons["Not Now"]   // iOS offers to save the password typed, though nothing was signed in to
        if notNow.waitForExistence(timeout: 5) { notNow.tap() }

        must(app.buttons["Try a demo lab"], "find the demo lab").tap()
        must(app.navigationBars["Demo lab"], "open the demo lab")
    }

    func testTheDemoLabFromEndToEnd() {
        launch(["-KelvinDemo", "YES"])

        // the lab: a request approved, an alarm acknowledged
        must(text(containing: "Raise the magnet"), "see the request")
        app.buttons["Approve"].firstMatch.tap()
        confirm("Approve", "approve the request")
        gone(text(containing: "Raise the magnet"), "answer the request")
        must(app.buttons["Got it"], "see the alarm").tap()
        gone(app.buttons["Got it"], "acknowledge the alarm")

        // the team chat: a name, a message
        app.tabBars.buttons["Team"].tap()
        must(app.navigationBars.buttons["Your name"], "find where the name is set").tap()
        must(app.alerts.textFields.firstMatch, "type a name").tap()
        app.typeText("Ana")
        app.alerts.buttons["Save"].tap()
        must(app.descendants(matching: .any).matching(identifier: "team-message").firstMatch, "find the message box").tap()
        app.typeText("Fridge at base")
        tap("Send", "send to the team")
        must(text(containing: "Fridge at base"), "see the message sent")
        gone(app.keyboards.firstMatch, "put the keyboard away on sending")   // else it covers the tabs

        // a chat with Kelvin
        app.tabBars.buttons["Chat"].tap()
        must(app.buttons["New chat"], "start a chat").tap()
        must(app.descendants(matching: .any).matching(identifier: "chat-message").firstMatch, "find the chat box").tap()
        app.typeText("What does the lock-in read?")
        tap("Send", "send to Kelvin")
        must(text(containing: "lock-in reads"), "get Kelvin's answer", timeout: 20)
        app.navigationBars.buttons.element(boundBy: 0).tap()
        must(app.buttons["New chat"], "go back to the chats")

        // a run, analysed: another y, a step, a fit kept, the CSV
        app.tabBars.buttons["Runs"].tap()
        must(app.staticTexts["field-sweep-D3"], "find the field sweep").tap()
        must(app.buttons["Analyze"], "find Analyze").tap()
        must(app.navigationBars["Analysis"], "open Analysis")
        tapRow("y, ", at: 0.9, "find y")   // its menu is on the right
        must(app.buttons["lockin.y (V)"], "choose another y").tap()
        must(app.buttons["y, lockin.y (V)"], "see the y chosen")
        tapRow("Add a step", at: 0.15, "find Add a step")
        must(app.buttons["Antisymmetrize"], "choose a step").tap()
        tapRow("Model", at: 0.9, "find the fit's model")
        must(app.buttons["linear"], "choose a model").tap()
        let list = app.collectionViews.firstMatch
        list.swipeDown()
        list.swipeDown()   // back up to the plot, where the fit shows
        let keep = must(app.buttons["Keep fit"], "see the fit", timeout: 20)
        reach(keep, "get to Keep fit").tap()
        must(app.buttons["Kept"], "keep the fit", timeout: 20)
        app.buttons["Export CSV"].tap()
        must(app.navigationBars.buttons["Done"], "see the CSV", timeout: 20).tap()
        must(app.navigationBars["Analysis"], "come back from the CSV")

        // the computers: the demo lab unpaired, the welcome screen again
        app.tabBars.buttons["Computers"].tap()
        let row = must(app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Demo lab'")).firstMatch,
                       "find the demo lab's row")
        row.swipeLeft()
        must(app.buttons["Unpair"], "find Unpair").tap()
        confirm("Unpair", "unpair the demo lab")
        must(app.buttons["Try a demo lab"], "get back to the welcome screen")
    }
}
