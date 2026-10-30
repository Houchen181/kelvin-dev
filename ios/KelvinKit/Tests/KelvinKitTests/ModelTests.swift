import XCTest
@testable import KelvinKit

func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
    try JSONDecoder().decode(T.self, from: Data(json.utf8))
}

final class ModelTests: XCTestCase {
    /// What /api/state answers (agent/runtime.py ui_state), trimmed.
    let state = """
    {"lab": "Cryostat lab", "simulated": false, "busy": "running iv-sweep",
     "pending": [{"id": "r1", "title": "Raise the field to 2 T", "items": ["magnet.field 0 T → 2 T"], "seconds_left": 290}],
     "history": [],
     "status": {"magnet": [{"name": "field", "value": "2 T", "away": true}],
                "lockin_1": [{"name": "x", "value": "1.2 µV", "away": false}, {"name": "freq", "value": "ERROR", "away": false}],
                "Cryostat": [{"name": "temperature", "value": "1.51 K", "away": false}]},
     "alarms": [{"id": "a1", "kind": "reading", "key": "cryostat.temperature", "text": "temperature above 4 K",
                 "raised": 1761140000.5, "actions": ["sent magnet.field to safe"], "cleared": null, "acknowledged": false},
                {"id": "a0", "kind": "reading", "key": "x", "text": "old", "raised": 1, "actions": [], "cleared": 2,
                 "acknowledged": false}],
     "notices": [{"id": 4, "kind": "run", "title": "iv-sweep finished", "text": "", "at": 1761140100}],
     "live": {"run_id": "20261022-101500-iv-sweep", "plan": "iv-sweep", "done": 12, "total": 40, "eta_s": 95.5,
              "status": "running"},
     "active": null, "approval_mode": "ask", "observe_only": false, "queue": {"items": []}, "watches": []}
    """

    func testTheLabAtAGlance() throws {
        let s = try decode(LabState.self, state)
        XCTAssertEqual(s.lab, "Cryostat lab")
        XCTAssertEqual(s.activity, "Kelvin is running iv-sweep")
        XCTAssertEqual(s.pending.first?.items, ["magnet.field 0 T → 2 T"])
        XCTAssertEqual(s.openAlarms.map(\.id), ["a1"])
        XCTAssertEqual(s.waiting, 2)
        XCTAssertEqual(s.instruments.map(\.name), ["Cryostat", "lockin_1", "magnet"])   // keys stay as Kelvin names them
        XCTAssertTrue(s.instruments[2].away)
        XCTAssertTrue(s.instruments[1].failing)
        XCTAssertEqual(s.live?.fraction, 0.3)
        XCTAssertEqual(s.live?.etaSeconds, 95.5)
        XCTAssertNil(s.active)
    }

    func testTheSampleItsProtectionAndTheLock() throws {
        let s = try decode(LabState.self, """
        {"lab": "Cryostat lab", "mounted": {"project": "p1", "project_name": "Graphene", "device": "d4",
         "device_name": "D4", "since": "2026-10-24T13:40:00", "by": "human",
         "protection": {"gates": []},
         "protection_lines": ["Gate smu.V within ±6 V (20 nm hBN), stops above 1 nA on smu.I"],
         "protection_missing": ["laser.power"]},
         "hands_on": {"by": "human (phone Ana's iPhone)", "since": "2026-10-24T14:02:11"}}
        """)
        XCTAssertEqual(s.mounted, Mounted(device: "D4", project: "Graphene",
                                          protection: ["Gate smu.V within ±6 V (20 nm hBN), stops above 1 nA on smu.I"],
                                          missing: ["laser.power"]))
        XCTAssertTrue(s.mounted?.isProtected == true)
        XCTAssertEqual(s.handsOn?.summary, "Locked at 14:02 from Ana's iPhone")
        XCTAssertEqual(HandsOn(by: "human (portal)", since: "").summary, "Locked from the Kelvin computer")
        let free = try decode(LabState.self, "{\"mounted\": {\"device_name\": \"D5\"}, \"hands_on\": null}")
        XCTAssertEqual(free.mounted?.project, "General")
        XCTAssertFalse(free.mounted?.isProtected ?? true)
        XCTAssertNil(free.handsOn)
        XCTAssertTrue(Role.control.canUnlock)
        XCTAssertFalse(Role.watch.canUnlock)
    }

    func testTheAccountsKelvins() throws {
        let hub = URL(string: "https://kelvin.example.org")!
        var list = ComputerList()
        let first = list.account(HubKelvin(id: "urn:uuid:ce8e:1a2b", name: "LAB-PC", version: "0.3.19"), hub: hub)
        XCTAssertTrue(first.viaAccount)
        XCTAssertEqual(first.relayBase?.absoluteString.removingPercentEncoding,
                       "https://kelvin.example.org/api/relay/kelvins/urn:uuid:ce8e:1a2b")
        XCTAssertEqual(list.selected, first.id)
        let again = list.account(HubKelvin(id: "urn:uuid:ce8e:1a2b", name: "LAB-PC-2"), hub: hub)
        XCTAssertEqual(again.id, first.id)                                         // the same one, renamed
        XCTAssertEqual(list.computers.map(\.host), ["LAB-PC-2"])
        let relay = try XCTUnwrap(first.relayBase)
        XCTAssertEqual(Route(relay, kinds: [relay.absoluteString: "hub"]), .hub)
        XCTAssertTrue(Route.hub.encrypted)
    }

    func testOneRowPerComputerFromTheAccount() {
        let hub = URL(string: "https://kelvin.example.org")!
        let listed = [HubKelvin(id: "a", name: "LAB-PC", online: true, lastSeen: 300),
                      HubKelvin(id: "b", name: "LAB-PC", online: false, lastSeen: 200),    // an older data folder
                      HubKelvin(id: "c", name: "LAPTOP", online: false, lastSeen: 100),
                      HubKelvin(id: "d", name: "LAPTOP", online: false, lastSeen: 150),    // its latest
                      HubKelvin(id: "e", name: "OLD-PC", online: false, lastSeen: 50)]
        var list = ComputerList()
        list.sync(listed, hub: hub)
        XCTAssertEqual(list.computers.compactMap(\.kelvin).sorted(), ["a", "d", "e"])
        XCTAssertEqual(list.sync(listed, hub: hub, removed: ["e"]).count, 1)            // removed on this phone
        XCTAssertEqual(list.computers.compactMap(\.kelvin).sorted(), ["a", "d"])
        list.sync(Array(listed.prefix(2)), hub: hub)                                    // LAPTOP signed out
        XCTAssertEqual(list.computers.compactMap(\.kelvin), ["a"])
        XCTAssertEqual(list.selected, list.computers.first?.id)
    }

    func testPairingTheSameComputerAgainReplacesItWhereverItIsNow() {
        let home = Computer(lab: "Cryostat lab", host: "LAB-PC", phoneName: "P", role: "control",
                            addresses: [URL(string: "http://10.0.0.5:8771")!])
        let away = Computer(lab: "Cryostat lab", host: "LAB-PC", phoneName: "P", role: "control",
                            addresses: [URL(string: "http://192.168.7.20:8771")!])
        var list = ComputerList()
        list.add(home)
        XCTAssertEqual(list.add(away), home.id)                                         // another network, one computer
        XCTAssertEqual(list.computers.map(\.id), [away.id])
        var before = ComputerList(computers: [home, away], selected: home.id)          // duplicates from app 0.1.6
        XCTAssertEqual(before.tidy(), [away.id])                                        // the one in use stays
        XCTAssertEqual(before.computers.map(\.id), [home.id])
        let other = Computer(lab: "Optics lab", host: "OPTICS-PC", phoneName: "P", role: "control",
                             addresses: [URL(string: "http://10.0.0.5:8771")!])
        list.add(other)
        XCTAssertEqual(list.computers.count, 2)                                         // another computer stays apart
    }

    func testAnOlderOrNewerKelvinStillReads() throws {
        let s = try decode(LabState.self, "{\"lab\": \"Virtual Lab\", \"something_new\": [1, 2]}")
        XCTAssertEqual(s.pending, [])
        XCTAssertEqual(s.activity, "Kelvin is idle")
        let r = try decode(Reading.self, "{\"name\": \"count\", \"value\": 3}")
        XCTAssertEqual(r.value, "3")
    }

    func testRunsAndTheirDetail() throws {
        let runs = try decode(RunsAnswer.self, """
        {"runs": [{"run_id": "20261022-101500-iv-sweep", "plan": "iv-sweep", "status": "completed", "points": 40,
                   "planned_points": 40, "reason": null},
                  {"run_id": "20261021-090000-cooldown", "plan": "cooldown", "status": "unfinished"}]}
        """).runs
        XCTAssertEqual(runs.map(\.status), ["completed", "unfinished"])
        let cal = Calendar(identifier: .gregorian)
        let started = cal.dateComponents([.year, .month, .day, .hour, .minute], from: runs[0].started!)
        XCTAssertEqual([started.year, started.month, started.day, started.hour, started.minute], [2026, 10, 22, 10, 15])

        let d = try decode(RunDetail.self, """
        {"run_id": "r", "plan": "iv", "axes": [{"key": "smu.voltage", "unit": "V"}], "scalars": [{"key": "smu.current", "unit": "A"}],
         "rows": [{"smu.voltage": 0, "smu.current": 1e-9}, {"smu.voltage": 0.1, "smu.current": null}],
         "traces": [], "plan_text": "plan: iv", "meta": {"status": "completed", "duration_s": 61.2, "warnings": ["slow"]},
         "files": [{"name": "report.pdf", "size": 1000}, {"name": "data.csv", "size": 10}], "analysis": null}
        """)
        XCTAssertEqual(d.files, ["report.pdf", "data.csv"])
        XCTAssertEqual(d.meta?.warnings, ["slow"])
        let s = Plot.series(rows: d.rows, x: d.axes[0], y: d.scalars[0])
        XCTAssertEqual(s.points, [Point(x: 0, y: 1e-9)])                            // the missing reading is skipped
    }

    func testModelsForANewChat() throws {
        let m = try decode(Models.self, """
        {"models": {"groups": [{"provider": "anthropic", "label": "Anthropic", "models": [
            {"key": "anthropic:claude-opus-5-5", "label": "Claude Opus 5.5"}]},
          {"provider": "openai", "label": "OpenAI", "models": [], "missing_key": true}]},
         "default_model": "anthropic:claude-opus-5-5", "chats": [], "settings": {}}
        """)
        XCTAssertEqual(m.all.map(\.label), ["Claude Opus 5.5"])
        XCTAssertEqual(m.label("anthropic:claude-opus-5-5"), "Claude Opus 5.5")
        XCTAssertEqual(m.label("google:gemini-x"), "gemini-x")
    }

    func testLiveDataGrowsAndStartsOverForANewRun() throws {
        func live(_ run: String, _ rows: String) throws -> LiveRun {
            try decode(LiveRun.self, """
            {"run_id": "\(run)", "plan": "iv", "total": 3, "done": 1, "status": "running",
             "axes": [{"key": "smu.voltage", "unit": "V", "kind": "sweep"}],
             "measures": [{"key": "smu.current", "unit": "A", "kind": "scalar"}, {"key": "spec.counts", "unit": "", "kind": "trace"}],
             "rows": \(rows), "since": 0}
            """)
        }
        var d = LiveData()
        XCTAssertFalse(d.take(try live("r1", "[{\"i\": 0, \"smu.voltage\": 0, \"smu.current\": 1}]"), since: 0))
        XCTAssertFalse(d.take(try live("r1", "[{\"i\": 1, \"smu.voltage\": 0.5, \"smu.current\": 2}]"), since: 1))
        XCTAssertEqual(d.since, 2)
        XCTAssertEqual(d.measured.map(\.key), ["smu.current"])
        XCTAssertEqual(d.series(d.measured[0]).points, [Point(x: 0, y: 1), Point(x: 0.5, y: 2)])
        XCTAssertEqual(d.series(d.measured[0]).x.key, "smu.voltage")
        XCTAssertTrue(d.take(try live("r2", "[{\"i\": 5, \"smu.voltage\": 1, \"smu.current\": 3}]"), since: 2))
        XCTAssertEqual(d.since, 0)                                                // fetch r2 again from its start
        XCTAssertEqual(d.runID, "r2")
    }

    func testAxesInSIUnits() {
        func scale(_ v: [Double], _ unit: String) -> String {
            let (f, u) = SI.scale(v, unit: unit)
            return "\(f) \(u)"
        }
        XCTAssertEqual(scale([3.1e-6, 2e-6], "V"), "1000000.0 µV")
        XCTAssertEqual(scale([0.5, -0.2], "V"), "1000.0 mV")
        XCTAssertEqual(scale([-4e-3], "A"), "1000.0 mA")
        XCTAssertEqual(scale([1500, 200], "Hz"), "0.001 kHz")
        XCTAssertEqual(scale([2.87e9], "Hz"), "1e-09 GHz")
        XCTAssertEqual(scale([1.52], "K"), "1.0 K")
        XCTAssertEqual(scale([0.012], "K"), "1000.0 mK")
        XCTAssertEqual(scale([3e-17], "V"), "1000000000000000.0 fV")                     // below the table: its last prefix
        XCTAssertEqual(scale([-20], "dBm"), "1.0 dBm")                     // no prefix on a logarithmic unit
        XCTAssertEqual(scale([3e-6], "µV"), "1.0 µV")                      // nor on one that has a prefix
        XCTAssertEqual(scale([0, .nan], "V"), "1.0 V")
        let s = Series(x: Column(key: "magnet.field", unit: "T"), y: Column(key: "lockin.x", unit: "V"),
                       points: [Point(x: -0.003, y: 3e-6), Point(x: 0.003, y: 2e-6)]).scaled()
        XCTAssertEqual([s.x.label, s.y.label], ["magnet.field (mT)", "lockin.x (µV)"])
        XCTAssertEqual(s.points[0].x, -3, accuracy: 1e-9)
        XCTAssertEqual(s.points[1].y, 2, accuracy: 1e-9)
    }

    func testTwoAxesPlotAgainstThePointNumber() {
        let x = Plot.xColumn([Column(key: "magnet.field", kind: "sweep"), Column(key: "smu.voltage", kind: "sweep")])
        XCTAssertEqual(x.key, "point")
        XCTAssertEqual(Plot.xColumn([Column(key: "repeat", kind: "repeat"), Column(key: "t", unit: "s", kind: "time")]).key, "t")
        let s = Plot.series(rows: [["i": .number(7), "y": .number(1)]], x: x, y: Column(key: "y"))
        XCTAssertEqual(s.points, [Point(x: 7, y: 1)])
    }
}

final class ComputerTests: XCTestCase {
    let lan = URL(string: "http://192.168.1.50:8771")!
    let tail = URL(string: "http://100.101.7.9:8771")!

    func testTheAddressThatWorkedGoesFirst() throws {
        var c = Computer(lab: "Virtual Lab", phoneName: "Phone", role: "watch", addresses: [lan])
        let me = try decode(Me.self, """
        {"role": "control", "name": "Ana's phone", "lab": "Cryostat lab", "computer": "LAB-PC", "version": "0.3.15",
         "api": 1, "addresses": ["http://100.101.7.9:8771", "http://192.168.1.50:8771"],
         "routes": [{"url": "http://100.101.7.9:8771", "kind": "tailscale"}, {"url": "http://192.168.1.50:8771", "kind": "network"}]}
        """)
        XCTAssertEqual(c.route(tail), .lab)                                     // before Kelvin said what it is
        c.update(with: me, used: lan)
        XCTAssertEqual(c.route(tail), .tailscale)
        XCTAssertTrue(c.route(tail).encrypted)
        XCTAssertEqual(c.addresses, [lan, tail])
        XCTAssertEqual([c.lab, c.host, c.phoneName, c.role, c.version], ["Cryostat lab", "LAB-PC", "Ana's phone", "control", "0.3.15"])
        c.worked(at: tail)
        XCTAssertEqual(c.addresses, [tail, lan])
    }

    func testPairingTheSameComputerAgainReplacesIt() {
        let when = Date(timeIntervalSince1970: 1_761_140_000)
        var list = ComputerList()
        let a = Computer(lab: "Cryostat lab", host: "LAB-PC", phoneName: "P", role: "watch", addresses: [lan], paired: when)
        let b = Computer(lab: "Optics lab", host: "OPTICS", phoneName: "P", role: "control", addresses: [URL(string: "http://10.0.0.2:8771")!], paired: when)
        XCTAssertNil(list.add(a))
        XCTAssertNil(list.add(b))
        let again = Computer(lab: "Cryostat lab", host: "LAB-PC", phoneName: "P", role: "control", addresses: [lan, tail], paired: when)
        XCTAssertEqual(list.add(again), a.id)
        XCTAssertEqual(list.computers.map(\.lab), ["Cryostat lab", "Optics lab"])
        XCTAssertEqual(list.current?.id, again.id)
        list.remove(again.id)
        XCTAssertEqual(list.current?.id, b.id)
        let back = ComputerList.decode(list.encoded())
        XCTAssertEqual(back, list)
        XCTAssertEqual(ComputerList.decode(Data("junk".utf8)), ComputerList())
    }

    func testTwoComputersOfOneLab() {
        let a = Computer(lab: "Cryostat lab", host: "PC-1", phoneName: "P", role: "watch", addresses: [lan])
        let b = Computer(lab: "Cryostat lab", host: "PC-2", phoneName: "P", role: "watch", addresses: [tail])
        XCTAssertEqual(a.title(among: [a, b]), "Cryostat lab (PC-1)")
        XCTAssertEqual(a.title(among: [a]), "Cryostat lab")
    }
}
