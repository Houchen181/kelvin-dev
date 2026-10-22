import XCTest
@testable import KelvinKit

final class TranscriptTests: XCTestCase {
    func testAConversationAsKelvinKeepsIt() throws {
        let chat = try decode(ChatDetail.self, """
        {"id": "c1", "title": "IV of D3", "model": "anthropic:claude-opus-5-5", "messages": [
          {"role": "user", "text": "Take an IV of D3 to 1 V"},
          {"role": "assistant", "blocks": [{"type": "thinking", "text": "plan"}, {"type": "text", "text": "Checking first."},
            {"type": "tool_call", "id": "t1", "name": "get_value", "input": {"instrument": "smu", "parameter": "voltage"}},
            {"type": "tool_call", "id": "t2", "name": "run_plan", "input": {"plan": "plan: iv-d3\\naxes: []"}}]},
          {"role": "tool", "results": [{"id": "t1", "name": "get_value", "content": "0 V", "is_error": false},
            {"id": "t2", "name": "run_plan", "content": "declined by Ana\\nnothing ran", "is_error": false}]},
          {"role": "assistant", "blocks": [{"type": "text", "text": "You declined, so nothing ran."}]}]}
        """)
        let t = Transcript(chat)
        XCTAssertEqual(t.title, "IV of D3")
        XCTAssertEqual(t.lines.map(\.kind), [.user, .assistant, .step, .step, .assistant])
        XCTAssertEqual(t.lines[2].text, "Read smu.voltage")
        XCTAssertEqual(t.lines[2].detail, "0 V")
        XCTAssertFalse(t.lines[2].failed)
        XCTAssertEqual(t.lines[3].text, "Ran plan iv-d3")
        XCTAssertTrue(t.lines[3].failed)                                         // declined: shown as not done
        XCTAssertFalse(t.lines.contains { $0.working })
    }

    func testAnAnswerAsItArrives() {
        var t = Transcript()
        t.say("Cool to 1.5 K")
        XCTAssertTrue(t.answering)
        t.apply(.started(model: "openai:gpt-6", title: "Cooldown"))
        t.apply(.memory(count: 2))
        t.apply(.text("Setting "))
        t.apply(.text("the setpoint."))
        t.apply(.toolStart(id: "t1", name: "set_value", input: .object(["instrument": .string("cryo"),
                                                                       "parameter": .string("setpoint"),
                                                                       "value": .number(1.5)])))
        t.apply(.approval(ApprovalRequest(id: "r9", title: "Lower the setpoint", items: ["cryo.setpoint 4 K → 1.5 K"])))
        t.apply(.approval(ApprovalRequest(id: "r9", title: "Lower the setpoint", items: [])))   // shown once
        XCTAssertEqual(t.lines.last?.answer, .waiting)
        t.answered("r9", approve: true)
        t.apply(.approvalClosed(id: "r9"))
        t.apply(.toolEnd(id: "t1", content: "set cryo.setpoint = 1.5 K", isError: false, runID: nil))
        t.apply(.text("Done."))
        t.apply(.ended(reason: "end"))

        XCTAssertEqual(t.title, "Cooldown")
        XCTAssertEqual(t.model, "openai:gpt-6")
        XCTAssertEqual(t.lines.map(\.kind), [.user, .note, .assistant, .step, .approval, .assistant])
        XCTAssertEqual(t.lines[1].text, "Kelvin recalled 2 notes")
        XCTAssertEqual(t.lines[2].text, "Setting the setpoint.")
        XCTAssertEqual(t.lines[3].text, "Set cryo.setpoint → 1.5")
        XCTAssertEqual(t.lines[3].detail, "set cryo.setpoint = 1.5 K")
        XCTAssertEqual(t.lines[4].answer, .approved)                             // the answer stays, not "closed"
        XCTAssertFalse(t.answering)
        XCTAssertFalse(t.lines.contains { $0.working })
    }

    func testRunProgressStopAndErrors() {
        var t = Transcript()
        t.say("run it")
        t.apply(.toolStart(id: "t2", name: "run_plan", input: .object(["plan": .string("plan: hall\n")])))
        t.apply(.runProgress(callID: "t2", done: 3, total: 10, status: "running"))
        XCTAssertEqual(t.lines.last?.detail, "3 of 10 points")
        XCTAssertTrue(t.lines.last?.working ?? false)
        t.apply(.reset)
        t.apply(.error("The model stopped answering."))
        t.apply(.ended(reason: "stopped"))
        XCTAssertEqual(t.lines.map(\.kind), [.user, .step, .error, .note])
        XCTAssertEqual(t.lines.last?.text, "Stopped.")
    }

    func testStepWords() {
        XCTAssertEqual(Steps.label("search_papers", .object(["query": .string("CrSBr magnons")])),
                       "Searched the literature for “CrSBr magnons”")
        XCTAssertEqual(Steps.label("remember", .object(["scope": .string("global"), "text": .string("Use 13 Hz")])),
                       "Remembered for every chat: Use 13 Hz")
        XCTAssertEqual(Steps.label("brand_new_tool", .null), "Brand new tool")
        XCTAssertEqual(Steps.summary("dry run: 40 points\nwould take 3 min"), "would take 3 min")
        XCTAssertTrue(Steps.refused("Refused: magnet.field above its limit"))
        XCTAssertTrue(Steps.refused("ok\nthe interlock tripped"))
        XCTAssertFalse(Steps.refused("set smu.voltage = 0.1 V"))
        XCTAssertFalse(Steps.refused("errors: none"))                            // a word, not a substring
    }

    func testEventsFromLines() {
        XCTAssertEqual(ChatEvent(line: "{\"type\": \"approval\", \"id\": \"r1\", \"title\": \"Go\", \"items\": [\"a\"], \"call_id\": \"t1\"}"),
                       .approval(ApprovalRequest(id: "r1", title: "Go", items: ["a"])))
        XCTAssertEqual(ChatEvent(line: "{\"type\": \"tool_end\", \"id\": \"t1\", \"name\": \"x\", \"content\": \"ok\", \"is_error\": true}"),
                       .toolEnd(id: "t1", content: "ok", isError: true, runID: nil))
        XCTAssertEqual(ChatEvent(line: "{\"type\": \"memory\", \"notes\": [{\"text\": \"a\"}]}"), .memory(count: 1))
        XCTAssertNil(ChatEvent(line: "{\"no_type\": 1}"))
        XCTAssertNil(ChatEvent(line: ""))
    }
}
