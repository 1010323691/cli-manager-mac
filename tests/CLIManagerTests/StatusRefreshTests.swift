import Foundation
import XCTest
@testable import CLIManager

final class StatusRefreshTests: XCTestCase {
    func testCausalStatusAndCombinedFailure() throws {
        func status(_ login: String, _ failure: Bool, _ quota: String, _ activity: String? = nil) throws -> InstanceRecord {
            let running = activity.map { [["id": "work", "state": $0]] } ?? []
            let value: [String: Any] = [
                "id": "fixture", "provider": "codex", "guard": "fixture", "parallel": 2,
                "login": login, "probe_failed": failure, "deleting": false, "running": running,
                "quota": ["state": quota, "windows": []]
            ]
            return try JSONDecoder().decode(InstanceRecord.self, from: JSONSerialization.data(withJSONObject: value))
        }
        XCTAssertEqual(try status("signed_in", false, "observed").activity.title, "空闲")
        XCTAssertEqual(try status("signed_out", false, "unknown").activity.title, "未登录")
        XCTAssertEqual(try status("expired", false, "unknown").activity.title, "登录失效")
        XCTAssertEqual(try status("unknown", true, "unknown").activity.title, "查询失败")
        let both = try status("unknown", true, "limited")
        XCTAssertEqual(both.activity.title, "额度耗尽")
        XCTAssertEqual(both.loginBadge.title, "查询失败，登录状态未知")
        XCTAssertEqual(try status("unknown", true, "limited", "running").activity.title, "执行中")
        XCTAssertEqual(try status("unknown", true, "limited", "stopping").activity.title, "停止中")
    }

    @MainActor
    func testReloadRequestedDuringOldPollDiscardsAndReadsAgain() async {
        let gate = SnapshotReload()
        var unblock: CheckedContinuation<Void, Never>?
        var calls = 0
        var published: [Int] = []
        let first = Task {
            await gate.request { current in
                calls += 1
                let value = calls
                if value == 1 { await withCheckedContinuation { unblock = $0 } }
                if current() { published.append(value) }
            }
        }
        while unblock == nil { await Task.yield() }
        let second = Task {
            await gate.request { _ in XCTFail("An older chain should coalesce the subsequent request") }
        }
        // Let the second request invalidate the first before its read completes.
        try? await Task.sleep(for: .milliseconds(40))
        unblock?.resume()
        await first.value
        await second.value
        XCTAssertEqual(calls, 2)
        XCTAssertEqual(published, [2])
    }
}
