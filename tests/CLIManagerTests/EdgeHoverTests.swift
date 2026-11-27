import XCTest
@testable import CLIManager

final class EdgeHoverTests: XCTestCase {
    func testDwellAndDeparture() {
        var interaction = EdgeHover()
        XCTAssertNil(interaction.sample(atTrigger: true, withinPanel: true, pointerDown: false,
                                        isOpen: false, isPinned: false, now: 0))
        XCTAssertNil(interaction.sample(atTrigger: true, withinPanel: true, pointerDown: false,
                                        isOpen: false, isPinned: false, now: 0.12))
        XCTAssertEqual(interaction.sample(atTrigger: true, withinPanel: true, pointerDown: false,
                                          isOpen: false, isPinned: false, now: 0.28), .open)
        XCTAssertNil(interaction.sample(atTrigger: false, withinPanel: true, pointerDown: false,
                                        isOpen: true, isPinned: false, now: 0.7))
        XCTAssertNil(interaction.sample(atTrigger: false, withinPanel: false, pointerDown: false,
                                        isOpen: true, isPinned: false, now: 1))
        XCTAssertEqual(interaction.sample(atTrigger: false, withinPanel: false, pointerDown: false,
                                          isOpen: true, isPinned: false, now: 1.42), .close)
    }

    func testPinnedPanelAndManualDismissal() {
        var interaction = EdgeHover()
        for now in [1.0, 4.0] {
            XCTAssertNil(interaction.sample(atTrigger: false, withinPanel: false, pointerDown: false,
                                            isOpen: true, isPinned: true, now: now))
        }
        interaction.collapsed()
        for now in [5.0, 10.0] {
            XCTAssertNil(interaction.sample(atTrigger: true, withinPanel: true, pointerDown: false,
                                            isOpen: false, isPinned: false, now: now))
        }
        XCTAssertNil(interaction.sample(atTrigger: false, withinPanel: false, pointerDown: false,
                                        isOpen: false, isPinned: false, now: 11))
        XCTAssertNil(interaction.sample(atTrigger: true, withinPanel: true, pointerDown: true,
                                        isOpen: false, isPinned: false, now: 12))
        XCTAssertNil(interaction.sample(atTrigger: true, withinPanel: true, pointerDown: false,
                                        isOpen: false, isPinned: false, now: 13))
        XCTAssertEqual(interaction.sample(atTrigger: true, withinPanel: true, pointerDown: false,
                                          isOpen: false, isPinned: false, now: 13.3), .open)
    }
}
