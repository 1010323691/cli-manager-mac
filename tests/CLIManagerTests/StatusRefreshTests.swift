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

    func testBoundInstanceWithoutUsableStatusIsQueriedForQuota() throws {
        func record(_ login: String, failed: Bool = false, group: String? = "group",
                    confirmed: String? = nil) throws -> InstanceRecord {
            var value: [String: Any] = [
                "id": "fixture", "provider": "codex", "guard": "fixture", "parallel": 2,
                "login": login, "probe_failed": failed, "deleting": false, "running": [],
                "quota": ["state": "unknown", "windows": []]
            ]
            if let group { value["quota_group"] = group }
            if let confirmed { value["last_confirmed_auth"] = ["login": confirmed, "checked_at": 1.0] }
            return try JSONDecoder().decode(InstanceRecord.self, from: JSONSerialization.data(withJSONObject: value))
        }
        XCTAssertTrue(try record("signed_in").hasQuotaAccount)
        // Cached status from another engine version, or never probed: the query settles it.
        XCTAssertTrue(try record("unknown").hasQuotaAccount)
        XCTAssertFalse(try record("unknown", group: nil).hasQuotaAccount)
        XCTAssertFalse(try record("unknown", failed: true).hasQuotaAccount)
        XCTAssertTrue(try record("unknown", failed: true, confirmed: "signed_in").hasQuotaAccount)
        for login in ["signed_out", "expired", "unbound", "rebind_required", "invalidated", "authenticating"] {
            XCTAssertFalse(try record(login).hasQuotaAccount, login)
        }
    }

    @MainActor
    func testAutomaticQuotaQueriesRunOnePerAccountSideBySide() {
        let candidates = [("a1", "A"), ("a2", "A"), ("b1", "B"), ("c1", "C"), ("d1", "D"), ("e1", "E")]
            .map { (instance: $0.0, group: $0.1) }
        // One instance per account, up to the limit.
        XCTAssertEqual(Dashboard.automaticQuotaBatch(candidates, busy: []), ["a1", "b1", "c1", "d1"])
        // Accounts already in flight are skipped and still occupy their slots.
        XCTAssertEqual(Dashboard.automaticQuotaBatch(candidates, busy: ["A", "C"]), ["b1", "d1"])
        XCTAssertEqual(Dashboard.automaticQuotaBatch(candidates, busy: ["A", "B", "C", "D"]), [])
        XCTAssertEqual(Dashboard.automaticQuotaBatch(candidates, busy: [], limit: 1), ["a1"])
        XCTAssertEqual(Dashboard.automaticQuotaBatch([], busy: []), [])
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
