import Foundation

/// A reload requested after a mutation must read again if a poll was in flight.
@MainActor
final class SnapshotReload {
    private var revision = 0
    private var inFlight: Task<Void, Never>?

    func request(_ read: @escaping (@escaping () -> Bool) async -> Void) async {
        revision += 1
        if let inFlight {
            await inFlight.value
            return
        }
        let operation = Task {
            defer { self.inFlight = nil }
            while true {
                let started = self.revision
                await read { self.revision == started }
                if self.revision == started { return }
            }
        }
        inFlight = operation
        await operation.value
    }
}
