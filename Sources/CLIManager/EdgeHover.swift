import Foundation

struct EdgeHover {
    enum Decision: Equatable { case open, close }

    private var enteredAt: TimeInterval?
    private var leftAt: TimeInterval?
    private var needsExit = false

    mutating func collapsed() {
        enteredAt = nil
        leftAt = nil
        needsExit = true
    }

    mutating func sample(
        atTrigger: Bool, withinPanel: Bool, pointerDown: Bool,
        isOpen: Bool, isPinned: Bool, now: TimeInterval
    ) -> Decision? {
        if !atTrigger { needsExit = false }
        if isOpen {
            guard !isPinned, !withinPanel else {
                leftAt = nil
                return nil
            }
            if let leftAt {
                return now - leftAt > 0.40 ? .close : nil
            }
            leftAt = now
        } else {
            guard atTrigger, !pointerDown, !needsExit else {
                enteredAt = nil
                return nil
            }
            if let enteredAt {
                if now - enteredAt > 0.25 {
                    leftAt = nil
                    return .open
                }
            } else { enteredAt = now }
        }
        return nil
    }
}
