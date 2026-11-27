import Foundation

struct QuotaResetEvent: Decodable, Hashable, Sendable {
    let bucket: String?
    let window: String?
    let resetsAt: Double

    enum CodingKeys: String, CodingKey {
        case bucket, window
        case resetsAt = "resets_at"
    }
}

struct QuotaResetRetries {
    private struct Backoff {
        let attempt: Int
        let retryAt: Double
    }

    private var byEvent: [QuotaResetEvent: Backoff] = [:]

    func allows(_ event: QuotaResetEvent, now: Double) -> Bool {
        event.resetsAt <= now && (byEvent[event]?.retryAt ?? 0) <= now
    }

    mutating func record(_ events: [QuotaResetEvent], now: Double) {
        let delays: [Double] = [60, 300, 900]
        for event in Set(events) {
            let attempt = min((byEvent[event]?.attempt ?? -1) + 1, delays.count - 1)
            byEvent[event] = Backoff(attempt: attempt, retryAt: now + delays[attempt])
        }
    }

    mutating func retain(_ events: Set<QuotaResetEvent>) {
        byEvent = byEvent.filter { events.contains($0.key) }
    }
}
