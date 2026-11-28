import CoreGraphics
import XCTest
@testable import CLIManager

final class EdgeGeometryTests: XCTestCase {
    func testPanelReservesExactlyTwoRows() {
        let screen = CGRect(x: 0, y: 0, width: 1440, height: 900)
        let geometry = EdgeGeometry(screen: screen, visible: screen.inset(by: CGRect.Inset(top: 24)), safeTop: 0)
        XCTAssertEqual(EdgeGeometry.activityHeight, 70)
        XCTAssertEqual(geometry.panel.height, 129)
    }

    func testExternalDisplayUsesItsOwnCoordinateSpace() {
        let screen = CGRect(x: -1920, y: 500, width: 1920, height: 1040)
        let geometry = EdgeGeometry(screen: screen, visible: screen.inset(by: CGRect.Inset(top: 24)), safeTop: 0)
        XCTAssertEqual(geometry.panel.midX, -960)
        XCTAssertEqual(geometry.panel.maxY, 1540)
        XCTAssertTrue(geometry.hitsTrigger(CGPoint(x: -960, y: 1540)))
        XCTAssertFalse(geometry.hitsTrigger(CGPoint(x: -960, y: 1541)))
        XCTAssertFalse(geometry.hitsTrigger(CGPoint(x: 960, y: 1540)))
    }

    func testTopSafeAreaAndMenuBarPolicy() {
        let screen = CGRect(x: 0, y: 0, width: 1440, height: 900)
        let fullScreen = EdgeGeometry(screen: screen, visible: screen, safeTop: 37)
        XCTAssertFalse(fullScreen.menuBarVisible)
        XCTAssertEqual(fullScreen.contentTopInset, 41)
        let desktop = EdgeGeometry(screen: screen, visible: screen.inset(by: CGRect.Inset(top: 37)), safeTop: 37)
        XCTAssertTrue(desktop.menuBarVisible)
        XCTAssertEqual(desktop.panel.maxY - desktop.contentTopInset, 859)
    }
}

private extension CGRect {
    struct Inset { let top: CGFloat }

    func inset(by value: Inset) -> CGRect {
        CGRect(x: minX, y: minY, width: width, height: height - value.top)
    }
}
