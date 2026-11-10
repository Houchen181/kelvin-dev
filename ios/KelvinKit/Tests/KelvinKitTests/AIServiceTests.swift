import XCTest
@testable import KelvinKit

/// Who a chat's messages go to, named before the first one goes (App Review guideline 5.1.2(i)), and the hub's
/// privacy notice linked in the app (5.1.1(i)).
final class AIServiceTests: XCTestCase {
    func testWhoAnswersEachOfKelvinsModels() {
        XCTAssertEqual(AIService.of(model: "hub-anthropic:claude-opus-5-5"), AIService(name: "Anthropic", viaHub: true))
        XCTAssertEqual(AIService.of(model: "anthropic:claude-opus-5-5"), AIService(name: "Anthropic"))
        XCTAssertEqual(AIService.of(model: "claudecode:sonnet")?.name, "Anthropic")
        XCTAssertEqual(AIService.of(model: "hub-openai:gpt-5.5"), AIService(name: "OpenAI", viaHub: true))
        XCTAssertEqual(AIService.of(model: "chatgpt:gpt-5.5")?.name, "OpenAI")
        XCTAssertEqual(AIService.of(model: "hub-gemini:gemini-3-pro")?.name, "Google")
        XCTAssertEqual(AIService.of(model: "hub-opencode-go:kimi-k2")?.name, "OpenCode")
        XCTAssertEqual(AIService.of(model: "openrouter:qwen/qwen3-coder")?.name, "OpenRouter")
        XCTAssertEqual(AIService.of(model: "my-gateway:llama-4", service: "My gateway")?.name, "My gateway")  // added
        XCTAssertEqual(AIService.of(model: "my-gateway:llama-4")?.name, "my-gateway")   // its name not known yet
        XCTAssertNil(AIService.of(model: "local:llama3.1"))   // a model server of the person's own
        XCTAssertNil(AIService.of(model: "demo:kelvin"))      // the demo lab answers inside the app
    }

    func testTheQuestionNamesTheCompanyTheModelAndTheComputer() {
        let s = AIService(name: "Anthropic", viaHub: true)
        XCTAssertEqual(s.question, "Send your chats to Anthropic?")
        let why = s.explanation(model: "Claude Opus 5.5", computer: "LAB-PC")
        XCTAssertTrue(why.hasPrefix("Kelvin on LAB-PC answers with Claude Opus 5.5, from Anthropic, through the Kelvin hub. "),
                      why)
        XCTAssertTrue(why.contains("goes to Anthropic"))
        XCTAssertFalse(AIService(name: "OpenAI").explanation(model: "GPT-5.5", computer: "LAB-PC").contains("hub"))
    }

    func testTheModelListKeepsEachModelsService() throws {
        let models = try decode(Models.self, """
        {"models": {"groups": [
          {"provider": "hub-anthropic", "label": "Claude · Kelvin hub",
           "models": [{"key": "hub-anthropic:claude-opus-5-5", "label": "Claude Opus 5.5"}]},
          {"provider": "my-gateway", "label": "My gateway", "models": [{"key": "my-gateway:llama-4", "label": "Llama 4"}]}]},
         "default_model": "hub-anthropic:claude-opus-5-5"}
        """)
        XCTAssertEqual(models.service(of: "my-gateway:llama-4"), "My gateway")
        XCTAssertEqual(AIService.of(model: "my-gateway:llama-4", service: models.service(of: "my-gateway:llama-4"))?.name,
                       "My gateway")
        XCTAssertEqual(models.label("hub-anthropic:claude-opus-5-5"), "Claude Opus 5.5")
        XCTAssertNil(models.service(of: "elsewhere:model"))
    }

    func testThePrivacyNoticeAndTermsAreOnTheHub() {
        let hub = URL(string: "https://kelvin.example.org")!
        XCTAssertEqual(Account.privacyNotice(hub).absoluteString, "https://kelvin.example.org/privacy")
        XCTAssertEqual(Account.terms(hub).absoluteString, "https://kelvin.example.org/terms")
    }
}
