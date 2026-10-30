import XCTest
@testable import KelvinKit
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

final class TeamTests: XCTestCase {
    let base = URL(string: "http://192.168.1.50:8771")!

    let page = """
    {"messages": [
      {"id": 7, "at": "2026-10-30T09:12:44-04:00", "name": "Ana", "account": "ana", "via": "portal", "text": "Fridge at base"},
      {"id": 8, "at": "2026-10-30T09:14:02-04:00", "name": "Ana", "account": "ana", "via": "portal", "text": "On D3 until 6"},
      {"id": 9, "at": "2026-10-30T11:40:00-04:00", "name": "Ben", "account": "ana", "via": "phone Ben's iPhone",
       "text": "He refill Thursday 9:00"}
    ], "last": 9}
    """

    func testTheTeamChatFromThePhone() async throws {
        let t = Scripted()
        t.on("GET /api/team", page)
        t.on("POST /api/team", "{\"message\": {\"id\": 10, \"at\": \"2026-10-30T12:00:00-04:00\", \"name\": \"Cleo\","
             + " \"account\": \"ana\", \"via\": \"phone Cleo's iPhone\", \"text\": \"Taking the magnet at 2\"}, \"last\": 10}")
        let c = KelvinClient(base: base, token: "tok", transport: t)
        let first = try await c.team()
        XCTAssertEqual(first.messages.map(\.id), [7, 8, 9])
        XCTAssertEqual(first.last, 9)
        XCTAssertNil(t.asked[0].url?.query)                                   // the latest messages
        _ = try await c.team(after: 9)
        XCTAssertEqual(t.asked[1].url?.query, "after=9")
        _ = try await c.team(before: 7)
        XCTAssertEqual(t.asked[2].url?.query, "before=7")
        let sent = try await c.postTeam(name: "Cleo", text: "Taking the magnet at 2")
        XCTAssertEqual(sent.id, 10)
        XCTAssertEqual(sent.via, "phone Cleo's iPhone")
        XCTAssertEqual(t.body(3)?["name"], .string("Cleo"))
        XCTAssertEqual(t.body(3)?["text"], .string("Taking the magnet at 2"))
    }

    func testARefusalSaysWhy() async {
        let t = Scripted()
        t.on("POST /api/team", 400, "{\"error\": \"say your name first\"}")
        do {
            _ = try await KelvinClient(base: base, token: "tok", transport: t).postTeam(name: " ", text: "hi")
            XCTFail("posted without a name")
        } catch let e as KelvinError {
            XCTAssertEqual(e.message, "say your name first")
        } catch {
            XCTFail("unexpected \(error)")
        }
    }

    func testTheThreadKeepsEachMessageOnceInOrder() throws {
        let first = try JSONDecoder().decode(TeamPage.self, from: Data(page.utf8))
        var thread = TeamThread()
        thread.take(first.messages)
        XCTAssertEqual(thread.last, 9)
        XCTAssertFalse(thread.more)                                          // less than a page: that is all of it
        thread.take([TeamMessage(id: 9, name: "Ben", text: "He refill Thursday 9:00"),
                     TeamMessage(id: 10, name: "Cleo", text: "Taking the magnet at 2")])
        XCTAssertEqual(thread.messages.map(\.id), [7, 8, 9, 10])
        thread.take([TeamMessage(id: 5, name: "Ana", text: "Cooling down")], older: true)
        XCTAssertEqual(thread.messages.map(\.id), [5, 7, 8, 9, 10])
        XCTAssertFalse(thread.more)

        var long = TeamThread()
        long.take((1...TeamThread.page).map { TeamMessage(id: 100 + $0, name: "Ana", text: "m\($0)") })
        XCTAssertTrue(long.more)                                              // a full page: older ones may be there
    }

    func testOwnMessageDoesNotSkipWhatOthersWroteJustBefore() throws {
        let first = try JSONDecoder().decode(TeamPage.self, from: Data(page.utf8))
        var thread = TeamThread()
        thread.take(first)
        XCTAssertEqual(thread.through, 9)
        thread.sent(TeamMessage(id: 11, name: "Cleo", text: "Taking the magnet"))   // Ben wrote 10 meanwhile
        XCTAssertEqual(thread.last, 11)
        XCTAssertEqual(thread.through, 9)                                     // so the next fetch asks after 9
        thread.take([TeamMessage(id: 10, name: "Ben", text: "Cooling down"), TeamMessage(id: 11, name: "Cleo", text: "Taking the magnet")])
        XCTAssertEqual(thread.messages.map(\.id), [7, 8, 9, 10, 11])
        XCTAssertEqual(thread.through, 11)
    }

    func testAChatThatBeganAgainOrRanAheadByAPage() throws {
        var thread = TeamThread()
        thread.take((1...40).map { TeamMessage(id: $0, name: "Ana", text: "m\($0)") })
        let restarted = try JSONDecoder().decode(TeamPage.self, from: Data("{\"messages\": [], \"last\": 3}".utf8))
        XCTAssertFalse(thread.take(restarted))                                // its newest is older than ours
        XCTAssertEqual(thread.messages, [])
        XCTAssertEqual(thread.through, 0)                                     // fetch again from the start

        thread.take((1...10).map { TeamMessage(id: $0, name: "Ana", text: "m\($0)") })
        thread.take((300..<(300 + TeamThread.page)).map { TeamMessage(id: $0, name: "Ben", text: "b\($0)") })
        XCTAssertEqual(thread.messages.first?.id, 300)                        // a full page: no silent gap before it
        XCTAssertTrue(thread.more)
        XCTAssertEqual(thread.through, 300 + TeamThread.page - 1)
    }

    func testADayStartsAndOnePersonRunsOn() throws {
        let first = try JSONDecoder().decode(TeamPage.self, from: Data(page.utf8))
        var thread = TeamThread()
        thread.take(first.messages)
        thread.take([TeamMessage(id: 11, at: "not a time", name: "Ben", text: "?")])
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/New_York")!
        let lines = thread.lines(calendar: calendar)
        XCTAssertEqual(lines[0].message.date, ISO8601DateFormatter().date(from: "2026-10-30T13:12:44Z"))
        XCTAssertNotNil(lines[0].day)                                         // the first of its day
        XCTAssertFalse(lines[0].continued)
        XCTAssertNil(lines[1].day)
        XCTAssertTrue(lines[1].continued)                                     // Ana again, 78 s later
        XCTAssertFalse(lines[2].continued)                                    // Ben
        XCTAssertNil(lines[3].day)                                            // a time that doesn't read: still shown
        XCTAssertEqual(lines.count, 4)
    }

    func testTheLabSaysItsNewestTeamMessage() throws {
        let now = try JSONDecoder().decode(LabState.self, from: Data("{\"lab\": \"Cryostat lab\", \"team\": 12}".utf8))
        XCTAssertEqual(now.team, 12)
        let older = try JSONDecoder().decode(LabState.self, from: Data("{\"lab\": \"Cryostat lab\"}".utf8))
        XCTAssertNil(older.team)                                              // a Kelvin before 0.3.32
        XCTAssertTrue(Role.control.canWriteTeam)
        XCTAssertFalse(Role.watch.canWriteTeam)
    }
}
