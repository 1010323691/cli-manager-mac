struct QuotaUsage: Decodable, Sendable {
    let started: Double?
    let ended: Double?

    static let empty = Self(started: nil, ended: nil)
}

struct QuotaLifecycle {
    private var previous: [String: QuotaUsage]?
    private(set) var pending: [String: Double] = [:]

    mutating func observe(_ current: [String: QuotaUsage]) {
        var newestByInstance = current
        if let previous {
            pending = pending.filter { current[$0.key] != nil }
            for (instance, usage) in current {
                guard let old = previous[instance] else { continue }
                let start = usage.started.flatMap { $0 > (old.started ?? 0) ? $0 : nil }
                let end = usage.ended.flatMap { $0 > (old.ended ?? 0) ? $0 : nil }
                let newest = max(start ?? 0, end ?? 0)
                if newest > (pending[instance] ?? 0) { pending[instance] = newest }
                newestByInstance[instance] = QuotaUsage(started: start ?? old.started, ended: end ?? old.ended)
            }
        }
        previous = newestByInstance
    }

    mutating func complete(_ instance: String, through timestamp: Double) {
        if let queued = pending[instance], queued <= timestamp { pending.removeValue(forKey: instance) }
    }
}
