import XCTest
@testable import CLIManager

final class ActivityAndQuotaTests: XCTestCase {
    func testActivityDeduplicatesTasksAndAttemptsAndSortsNewestFirst() {
        let running = LiveAttempt(taskID: "one", attemptID: "new", instance: "codex1", state: "running", since: 10)
        let older = LiveAttempt(taskID: "one", attemptID: "old", instance: "codex1", state: "running", since: 1)
        let duplicateID = LiveAttempt(taskID: "other", attemptID: "new", instance: "codex2", state: "stopping", since: 2)
        let stopping = LiveAttempt(taskID: "two", attemptID: "stop", instance: "codex2", state: "stopping", since: nil)
        let unverified = LiveAttempt(taskID: "three", attemptID: "unknown", instance: "codex3", state: "unverified", since: 15)
        XCTAssertEqual(LiveAttempt.normalized([older, running, duplicateID, unverified, stopping, running]),
                       [running, stopping])
    }

    func testSidebarUsesActualWindowsWithStableSlotsAndNoFabricatedWeek() {
        let short = window("primary", minutes: 300)
        let weekly = window("secondary", minutes: 10080)
        let custom = window("session", minutes: nil, bucket: "claude")
        XCTAssertEqual(QuotaReading.split([weekly, short]).slots.map { $0?.shortLabel }, ["5h", "7d"])
        XCTAssertEqual(QuotaReading.split([short]).slots.map { $0?.shortLabel }, ["5h", nil])
        XCTAssertEqual(QuotaReading.split([weekly]).slots.map { $0?.shortLabel }, [nil, "7d"])
        XCTAssertEqual(QuotaReading.split([custom]).slots.map { $0?.shortLabel }, ["会话", nil])
        XCTAssertEqual(QuotaReading.split([weekly, short, custom]).extras.map(\.id), [custom.id])
    }

    func testUnknownCheckingExpiredAndExhaustedUseDifferentReadings() {
        func reading(_ source: QuotaWindow?, checking: Bool = false, failed: Bool = false,
                     unavailable: Bool = false) -> QuotaReading {
            QuotaReading(window: source, provider: .codex, querying: checking, failed: failed,
                         snapshotUnavailable: unavailable)
        }
        let fresh = window("primary", minutes: 300)
        let expired = window("primary", minutes: 300, expired: true)
        let hard = window("primary", minutes: 300, remaining: 0)
        XCTAssertEqual(reading(fresh).remaining, 76)
        XCTAssertEqual(reading(nil).value, "无数据")
        XCTAssertEqual(reading(expired).value, "过期")
        XCTAssertEqual(reading(fresh, checking: true).value, "查询中")
        XCTAssertEqual(reading(fresh, failed: true).value, "未知")
        XCTAssertNil(reading(fresh, failed: true).remaining)
        XCTAssertTrue(reading(hard, failed: true).isExhausted)
        XCTAssertEqual(reading(hard, failed: true).remaining, 0)
        XCTAssertNil(reading(hard, unavailable: true).remaining)
        XCTAssertTrue(reading(hard).explanation.contains("重置"))
        XCTAssertTrue(reading(hard).explanation.contains("Codex account/rateLimits/read"))
    }

    func testQuotaLifecycleTracksStartAndFinishWithoutReplayingHistory() {
        var lifecycle = QuotaLifecycle()
        lifecycle.observe(["binding": QuotaUsage(started: 1, ended: 2)])
        XCTAssertTrue(lifecycle.pending.isEmpty)
        lifecycle.observe(["binding": QuotaUsage(started: 3, ended: 2)])
        XCTAssertEqual(lifecycle.pending["binding"], 3)
        lifecycle.observe(["binding": QuotaUsage(started: 3, ended: 4)])
        lifecycle.complete("binding", through: 3)
        XCTAssertEqual(lifecycle.pending["binding"], 4)
        lifecycle.complete("binding", through: 4)
        lifecycle.observe(["binding": .empty])
        lifecycle.observe(["binding": QuotaUsage(started: 3, ended: 4)])
        XCTAssertTrue(lifecycle.pending.isEmpty)
        lifecycle.observe(["replacement": QuotaUsage(started: 3, ended: 4)])
        XCTAssertTrue(lifecycle.pending.isEmpty)
    }

    func testEachQuotaWindowHasIndependentResetBackoff() {
        let primary = QuotaResetEvent(bucket: "codex", window: "primary", resetsAt: 100)
        let secondary = QuotaResetEvent(bucket: "codex", window: "secondary", resetsAt: 180)
        var retries = QuotaResetRetries()
        retries.record([primary], now: 101)
        XCTAssertFalse(retries.allows(primary, now: 160))
        XCTAssertTrue(retries.allows(primary, now: 161))
        retries.record([primary], now: 161)
        XCTAssertFalse(retries.allows(primary, now: 180))
        XCTAssertFalse(retries.allows(secondary, now: 179))
        XCTAssertTrue(retries.allows(secondary, now: 180))
        retries.record([primary, secondary], now: 180)
        XCTAssertFalse(retries.allows(primary, now: 240))
        XCTAssertTrue(retries.allows(secondary, now: 240))
        let replacement = QuotaResetEvent(bucket: "codex", window: "primary", resetsAt: 400)
        retries.retain([secondary, replacement])
        XCTAssertTrue(retries.allows(replacement, now: 400))
    }

    private func window(_ label: String, minutes: Double?, bucket: String = "codex",
                        remaining: Double = 76, expired: Bool = false) -> QuotaWindow {
        QuotaWindow(bucket: bucket, window: label, remaining: remaining, observedAt: 100, resetsAt: 120,
                    minutes: minutes, expired: expired)
    }
}
