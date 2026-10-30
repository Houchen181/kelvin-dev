import XCTest
@testable import KelvinKit
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

final class AnalysisTests: XCTestCase {
    let base = URL(string: "http://192.168.1.50:8771")!

    func testWhatARunOffers() throws {
        let json = """
        {"run": "20261030-101500-map", "plan": "map", "status": "completed", "points": 121,
         "axes": [{"key": "magnet.B", "unit": "T"}, {"key": "smu.V", "unit": "V"}],
         "readings": [{"key": "lockin.x", "unit": "V"}, {"key": "lockin.y", "unit": "V"}],
         "traces": [{"key": "vna.s21", "unit": "dB", "x_unit": "Hz"}],
         "kept": ["lockin.x linear", null], "models": ["linear", "lorentzian", "resonator"]}
        """
        let c = try JSONDecoder().decode(AnalysisColumns.self, from: Data(json.utf8))
        XCTAssertEqual(c.defaultX, "smu.V")                                   // the innermost swept axis
        XCTAssertEqual(c.defaultY, "lockin.x")
        XCTAssertEqual(c.defaultGroup, "magnet.B")                            // a map: a curve per field
        XCTAssertEqual(c.traces.first?.xUnit, "Hz")
        XCTAssertTrue(c.isTrace("vna.s21"))
        XCTAssertEqual(c.kept, ["lockin.x linear"])
        XCTAssertEqual(c.models, ["linear", "lorentzian"])                    // a resonator's fit needs its phase
        let bare = try JSONDecoder().decode(AnalysisColumns.self, from: Data("{\"run\": \"r1\"}".utf8))
        XCTAssertEqual(bare.defaultX, "t")                                    // nothing swept: the time
        XCTAssertEqual(bare.defaultGroup, "")
    }

    func testASpecAsKelvinReadsIt() throws {
        var spec = AnalysisSpec(runs: ["r1", "r2"], x: "magnet.B", y: "lockin.y / smu.I", condition: " ")
        spec.steps = [AnalysisStep(.antisymmetrize), AnalysisStep(.crop, ["lo": "−2", "hi": "2,5"]),
                      AnalysisStep(.smooth), AnalysisStep(.bin, ["width": "abc"]), AnalysisStep(.normalize)]
        spec.fit = AnalysisFit(model: "linear", lo: "0")
        let json = spec.json
        XCTAssertEqual(json["runs"], .array([.string("r1"), .string("r2")]))
        XCTAssertEqual(json["y"], .string("lockin.y / smu.I"))
        XCTAssertNil(json["where"])                                           // left empty: left out
        XCTAssertNil(json["group"])
        let steps = json["steps"]?.array ?? []
        XCTAssertEqual(steps.count, 5)
        XCTAssertEqual(steps[0], .object(["op": .string("antisymmetrize")]))
        XCTAssertEqual(steps[1]["lo"], .number(-2))                           // a typographic minus
        XCTAssertEqual(steps[1]["hi"], .number(2.5))                          // a decimal comma
        XCTAssertEqual(steps[2]["points"], .number(5))                        // its default
        XCTAssertEqual(steps[3]["width"], .string("abc"))                     // Kelvin says what is wrong with it
        XCTAssertEqual(steps[4]["how"], .string("max"))
        XCTAssertEqual(json["fit"], .object(["model": .string("linear"), "lo": .number(0)]))
        XCTAssertEqual(AnalysisSpec(json: json).json, json)                   // read back as it was written
        spec.fit = AnalysisFit(model: " ")
        XCTAssertNil(spec.json["fit"])                                        // no model: no fit
    }

    func testWhatKelvinWorkedOut() throws {
        let json = """
        {"curves": [{"run": "r1", "label": "r1", "n": 5, "x": [-2, -1, 0, 1, 2], "y": [1e-6, null, 3e-6, 4e-6, 5e-6],
                     "fit": {"model": "linear", "formula": "a + b*x", "n": 5, "chi2_red": 1.1, "r2": 0.998,
                             "converged": true, "derived": [], "warnings": [], "text": "a = ...",
                             "params": [{"name": "a", "value": 3e-6, "error": 1e-8, "unit": "V", "held": false},
                                        {"name": "b", "value": 1e-6, "error": 2.5e-9, "unit": "V/T", "held": true}],
                             "curve": {"x": [-2, 2], "y": [1e-6, 5e-6]}}},
                    {"run": "r2", "label": "r2 · magnet.B = 1 T", "n": 2, "x": [0, 1], "y": [2e-6, 9e-6],
                     "fit": {"error": "not fitted: too few points"}}],
         "axes": {"x": "magnet.B", "x_unit": "T", "y": "lockin.y (antisymmetric part)", "y_unit": "V"},
         "notes": ["30 curves; the first 24 are drawn"]}
        """
        let r = try JSONDecoder().decode(AnalysisResult.self, from: Data(json.utf8))
        XCTAssertEqual(r.curves[0].points.count, 4)                           // null is no point
        XCTAssertEqual(r.curves[0].count, 5)
        XCTAssertEqual(r.curves[0].fit?.params.map(\.name), ["a", "b"])
        XCTAssertEqual(r.curves[0].fit?.params.last?.held, true)
        XCTAssertEqual(r.curves[0].fit?.r2, 0.998)
        XCTAssertEqual(r.curves[0].fit?.chi2, 1.1)
        XCTAssertEqual(r.curves[1].fit?.error, "not fitted: too few points")
        XCTAssertEqual(r.x.label, "magnet.B (T)")
        XCTAssertEqual(r.notes, ["30 curves; the first 24 are drawn"])
        let d = r.drawn()
        XCTAssertEqual(d.y.unit, "µV")                                        // both curves in one scale
        XCTAssertEqual(d.lines[1].points.last?.y ?? 0, 9, accuracy: 1e-9)
        XCTAssertEqual(d.lines[0].fit.count, 2)
        XCTAssertEqual(d.lines[0].label, "r1")
    }

    func testALongCurveIsThinnedKeepingItsPeak() {
        let points = (0..<10000).map { Point(x: Double($0), y: $0 == 4321 ? 50 : sin(Double($0) / 300)) }
        let thin = Drawn.thin(points, to: 800)
        XCTAssertLessThanOrEqual(thin.count, 800)
        XCTAssertTrue(thin.contains(Point(x: 4321, y: 50)))                  // a narrow peak stays
        XCTAssertEqual(thin.map(\.x), thin.map(\.x).sorted())                // in order
        XCTAssertEqual(Drawn.thin(Array(points.prefix(10)), to: 800).count, 10)
    }

    func testAnalysisAsksWithTheSpecAndWaitsForFits() async throws {
        let t = Scripted()
        t.on("GET /api/analysis/columns", "{\"run\": \"r1\", \"axes\": [], \"readings\": [{\"key\": \"smu.I\", \"unit\": \"A\"}]}")
        t.on("POST /api/analysis", "{\"curves\": [], \"axes\": {}, \"notes\": []}")
        let c = KelvinClient(base: base, token: "tok", transport: t)
        let cols = try await c.analysisColumns(run: "20261030-101500-iv")
        XCTAssertEqual(cols.defaultY, "smu.I")
        XCTAssertEqual(t.asked[0].url?.query, "run=20261030-101500-iv")
        _ = try await c.analyze(AnalysisSpec(runs: ["r1"], y: "smu.I"))
        XCTAssertEqual(t.body(1)?["spec"]?["y"], .string("smu.I"))
        XCTAssertEqual(t.asked[1].timeoutInterval, 90)                        // fits may take Kelvin 45 s
    }

    func testKeepingAFit() async throws {
        let t = Scripted()
        t.on("POST /api/analysis/keep", 409, "{\"error\": \"a fit named 'lockin.y linear' is kept with this run already\","
             + " \"exists\": \"lockin.y linear\"}")
        let c = KelvinClient(base: base, token: "tok", transport: t)
        let spec = AnalysisSpec(runs: ["r1"], y: "lockin.y", fit: AnalysisFit(model: "linear"))
        let first = try await c.keepFit(spec, curve: 0)
        XCTAssertEqual(first, .exists("lockin.y linear"))
        XCTAssertEqual(t.body(0)?["replace"], .bool(false))
        XCTAssertEqual(t.body(0)?["curve"], .number(0))
        XCTAssertEqual(t.body(0)?["spec"]?["fit"]?["model"], .string("linear"))
        t.on("POST /api/analysis/keep", "{\"ok\": true, \"said\": \"kept in C:/Kelvin/runs/r1/run.h5 as /fits/lockin.y"
             + " linear; its scan report shows it now (report.pdf)\"}")
        let again = try await c.keepFit(spec, curve: 0, replace: true)
        XCTAssertEqual(t.body(1)?["replace"], .bool(true))
        guard case .kept(let said) = again else { return XCTFail("not kept") }
        XCTAssertEqual(KeepAnswer.name(said), "lockin.y linear")
        XCTAssertNil(KeepAnswer.name("kept"))
        t.on("POST /api/analysis/keep", 400, "{\"error\": \"fit a model first\"}")
        do {
            _ = try await c.keepFit(spec, curve: 0)
            XCTFail("kept without a fit")
        } catch let e as KelvinError {
            XCTAssertEqual(e.message, "fit a model first")
        }
    }

    func testTheCSVKeepsKelvinsName() async throws {
        let t = Scripted()
        t.on("POST /api/analysis/csv", 200, "magnet.B [T],lockin.y [V]\n0.0,1e-07\n",
             headers: ["Content-Type": "text/csv; charset=utf-8",
                       "Content-Disposition": "attachment; filename=\"field_sweep-analysis.csv\"; "
                           + "filename*=UTF-8''field%C2%B5sweep-analysis.csv"])
        let c = KelvinClient(base: base, token: "tok", transport: t)
        let (name, data) = try await c.analysisCSV(AnalysisSpec(runs: ["20261030-field-sweep"]))
        XCTAssertEqual(name, "fieldµsweep-analysis.csv")
        XCTAssertTrue(String(decoding: data, as: UTF8.self).hasPrefix("magnet.B [T]"))
        XCTAssertEqual(t.asked[0].timeoutInterval, 90)
        XCTAssertEqual(KelvinClient.fileName(contentDisposition: "attachment; filename=\"x.csv\""), "x.csv")
        XCTAssertEqual(KelvinClient.fileName(contentDisposition: "attachment; filename*=UTF-8''..%2F..%2Fetc%2Fpasswd"),
                       "passwd")
        XCTAssertNil(KelvinClient.fileName(contentDisposition: "attachment"))
        let bare = Scripted()
        bare.on("POST /api/analysis/csv", "a,b\n")
        let (fallback, _) = try await KelvinClient(base: base, token: "tok", transport: bare)
            .analysisCSV(AnalysisSpec(runs: ["r9"]))
        XCTAssertEqual(fallback, "r9-analysis.csv")                           // no name given: the run's
    }

    func testFittedValuesAsAPhysicistWritesThem() {
        XCTAssertEqual(Measured.text(3.21043e-6, error: 1.2e-8, unit: "V"), "3.210 ± 0.012 µV")
        XCTAssertEqual(Measured.text(4.0e-7, error: 2.5e-9, unit: "V/T"), "(4.000 ± 0.025)e-7 V/T")
        XCTAssertEqual(Measured.text(1234.0, error: 35, unit: "Ω"), "1.234 ± 0.035 kΩ")
        XCTAssertEqual(Measured.text(0.98, error: 0.0123), "0.980 ± 0.012")
        XCTAssertEqual(Measured.text(0.5), "0.5")
        XCTAssertEqual(Measured.text(2.0, error: 0, unit: "K"), "2 K")      // no error to give
        XCTAssertEqual(Measured.text(nil), "–")
    }

    func testWhichKelvinHasTheTeamChatAndAnalysis() {
        XCTAssertEqual(KelvinVersion.atLeast("0.3.32", "0.3.32"), true)
        XCTAssertEqual(KelvinVersion.atLeast("0.3.31", "0.3.32"), false)
        XCTAssertEqual(KelvinVersion.atLeast("0.4", "0.3.32"), true)
        XCTAssertEqual(KelvinVersion.atLeast("v0.3.40-private", "0.3.32"), true)
        XCTAssertNil(KelvinVersion.atLeast("demo", "0.3.32"))
        XCTAssertNil(KelvinVersion.atLeast(nil, "0.3.32"))
        var c = Computer(lab: "Cryostat lab", phoneName: "P", role: "control", addresses: [], version: "0.3.21")
        XCTAssertFalse(c.hasTeamAndAnalysis)
        c.version = nil
        XCTAssertTrue(c.hasTeamAndAnalysis)                                   // not known: try it
    }
}
