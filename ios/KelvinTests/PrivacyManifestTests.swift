import XCTest
@testable import Kelvin

/// App Review reads the app's privacy manifest: it must be inside the app, say there is no tracking, give the reason
/// the app uses UserDefaults (its own settings: CA92.1), and list the data the App Privacy answers in docs/ios.md do.
final class PrivacyManifestTests: XCTestCase {
    private func manifest() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "PrivacyInfo", withExtension: "xcprivacy"),
                                "PrivacyInfo.xcprivacy is not in the app")
        let data = try Data(contentsOf: url)
        let plist = try PropertyListSerialization.propertyList(from: data, format: nil)
        return try XCTUnwrap(plist as? [String: Any])
    }

    func testTheAppCarriesItsPrivacyManifestWithTheReasonForUserDefaults() throws {
        let m = try manifest()
        XCTAssertEqual(m["NSPrivacyTracking"] as? Bool, false)
        XCTAssertEqual((m["NSPrivacyTrackingDomains"] as? [String])?.count, 0)
        let apis = try XCTUnwrap(m["NSPrivacyAccessedAPITypes"] as? [[String: Any]])
        let defaults = apis.first {
            $0["NSPrivacyAccessedAPIType"] as? String == "NSPrivacyAccessedAPICategoryUserDefaults"
        }
        XCTAssertEqual(defaults?["NSPrivacyAccessedAPITypeReasons"] as? [String], ["CA92.1"])
    }

    func testTheManifestListsTheDataTheAppPrivacyAnswersDo() throws {
        let m = try manifest()
        let types = try XCTUnwrap(m["NSPrivacyCollectedDataTypes"] as? [[String: Any]])
        let named = Set(types.compactMap { $0["NSPrivacyCollectedDataType"] as? String })
        XCTAssertEqual(named, ["NSPrivacyCollectedDataTypeName", "NSPrivacyCollectedDataTypeEmailAddress",
                               "NSPrivacyCollectedDataTypeUserID", "NSPrivacyCollectedDataTypeDeviceID",
                               "NSPrivacyCollectedDataTypeOtherUserContent"])
        for t in types {   // all linked to the person, none used for tracking
            XCTAssertEqual(t["NSPrivacyCollectedDataTypeLinked"] as? Bool, true)
            XCTAssertEqual(t["NSPrivacyCollectedDataTypeTracking"] as? Bool, false)
            XCTAssertFalse((t["NSPrivacyCollectedDataTypePurposes"] as? [String] ?? []).isEmpty)
        }
    }
}
