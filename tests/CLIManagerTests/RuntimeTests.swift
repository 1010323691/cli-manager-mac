import Foundation
import XCTest
@testable import CLIManager

final class RuntimeTests: XCTestCase {
    func testInheritedStdoutDoesNotHoldCommandOpen() throws {
        let start = ProcessInfo.processInfo.systemUptime
        let result = try Runtime.execute(URL(fileURLWithPath: "/bin/sh"),
                                         ["-c", "printf ready; /bin/sleep 4 & exit 0"],
                                         environment: ProcessInfo.processInfo.environment, timeout: 5)
        XCTAssertEqual(result.status, 0)
        XCTAssertEqual(String(data: result.data, encoding: .utf8), "ready")
        XCTAssertLessThan(ProcessInfo.processInfo.systemUptime - start, 2)
    }

    func testCollectsTrailingOutputFromExitingChild() throws {
        for _ in 0..<20 {
            let result = try Runtime.execute(URL(fileURLWithPath: "/bin/sh"),
                                             ["-c", "printf '{\"state\":'; printf '\"ready\"}'"],
                                             environment: ProcessInfo.processInfo.environment, timeout: 2)
            XCTAssertEqual(result.status, 0)
            XCTAssertEqual(String(data: result.data, encoding: .utf8), #"{"state":"ready"}"#)
        }
    }

    func testTimeoutTerminatesChild() {
        let start = ProcessInfo.processInfo.systemUptime
        XCTAssertThrowsError(try Runtime.execute(URL(fileURLWithPath: "/bin/sleep"), ["10"],
                                                  environment: ProcessInfo.processInfo.environment, timeout: 0.05)) {
            guard case CLIError.timeout = $0 else { return XCTFail("Unexpected error: \($0)") }
        }
        XCTAssertLessThan(ProcessInfo.processInfo.systemUptime - start, 2)
    }
}
