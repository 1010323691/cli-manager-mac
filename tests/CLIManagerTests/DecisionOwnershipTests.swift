import XCTest
@testable import CLIManager

final class DecisionOwnershipTests: XCTestCase {
    func testOnlyExplicitlyOpenedAndFocusedNativeTerminalReceivesItsRequest() {
        var router = DirectTerminalDecisionRouter()
        var unrelatedRouter = DirectTerminalDecisionRouter()
        let untrusted = unrelatedRouter.userOpenedTerminal()
        let target = router.userOpenedTerminal()
        let other = router.userOpenedTerminal()

        let question = DirectTerminalDecisionRouter.Request(id: "provider-1", kind: .question)
        let permission = DirectTerminalDecisionRouter.Request(id: "provider-2", kind: .permission)

        XCTAssertNil(router.notice(for: question, from: target))
        router.focus(target)
        XCTAssertNil(router.notice(for: question, from: untrusted))
        XCTAssertNil(router.notice(for: question, from: other))
        XCTAssertEqual(router.notice(for: question, from: target)?.target, target)
        XCTAssertNil(router.notice(for: question, from: target))

        router.focus(other)
        XCTAssertNil(router.notice(for: permission, from: target))
        XCTAssertEqual(router.notice(for: permission, from: other)?.target, other)
        router.close(other)
        router.focus(other)
        XCTAssertNil(router.notice(for: question, from: other))
    }

    func testPassiveDelegationInspectionHasNoRequestPayload() throws {
        let data = Data("""
        {"schema":1,"entries":[{"id":"owned-task","route":"planner","questions":1,"permissions":1},
        {"id":"unbound-task","route":"unknown","questions":0,"permissions":1}]}
        """.utf8)
        let entries = try JSONDecoder().decode(DecisionInspection.self, from: data).entries
        XCTAssertEqual(entries.map(\.route), ["planner", "unknown"])
        XCTAssertEqual(entries.map(\.questions), [1, 0])
    }
}
