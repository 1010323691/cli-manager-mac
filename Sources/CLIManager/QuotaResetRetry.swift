import Foundation

struct QuotaResetRetry {
    let reset: Double
    private let attempt: Int
    private let retryAt: Double

    init(reset: Double, previous: Self?, now: Double) {
        self.reset = reset
        let delays: [Double] = [60, 300, 900]
        attempt = previous?.reset == reset ? min((previous?.attempt ?? 0) + 1, delays.count - 1) : 0
        retryAt = now + delays[attempt]
    }

    func allows(_ currentReset: Double, now: Double) -> Bool {
        currentReset != reset || retryAt <= now
    }
}
