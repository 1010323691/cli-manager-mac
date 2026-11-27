import Foundation
import XCTest
@testable import CLIManager

final class TerminalDirectoryTests: XCTestCase {
    func testOnlyExistingIndependentDirectoriesAreAccepted() throws {
        let fs = FileManager.default
        let base = fs.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let state = base.appendingPathComponent("state", isDirectory: true)
        let inside = state.appendingPathComponent("inside", isDirectory: true)
        let workspace = base.appendingPathComponent("Chosen Work Directory", isDirectory: true)
        let missing = base.appendingPathComponent("missing", isDirectory: true)
        let alias = base.appendingPathComponent("alias")
        let file = base.appendingPathComponent("file.txt")
        try fs.createDirectory(at: inside, withIntermediateDirectories: true)
        defer { try? fs.removeItem(at: base) }
        try fs.createDirectory(at: workspace, withIntermediateDirectories: true)
        try fs.createSymbolicLink(at: alias, withDestinationURL: inside)
        try Data().write(to: file)
        XCTAssertEqual(TerminalDirectory.validated(workspace, state: state)?.path,
                       workspace.resolvingSymlinksInPath().path)
        for invalid in [base, state, inside, alias, missing, file] {
            XCTAssertNil(TerminalDirectory.validated(invalid, state: state), invalid.path)
        }
    }

    func testRejectsCaseAliasesOfStateAndAncestors() throws {
        let fs = FileManager.default
        let base = fs.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let parent = base.appendingPathComponent("Workspace", isDirectory: true)
        let state = parent.appendingPathComponent("State", isDirectory: true)
        let child = state.appendingPathComponent("Child", isDirectory: true)
        try fs.createDirectory(at: child, withIntermediateDirectories: true)
        defer { try? fs.removeItem(at: base) }
        let alias = base.appendingPathComponent("workspace", isDirectory: true)
        try XCTSkipUnless(fs.fileExists(atPath: alias.path), "requires a case-insensitive filesystem")
        let stateAlias = alias.appendingPathComponent("STATE", isDirectory: true)
        XCTAssertNil(TerminalDirectory.validated(alias, state: state))
        XCTAssertNil(TerminalDirectory.validated(stateAlias, state: state))
        XCTAssertNil(TerminalDirectory.validated(stateAlias.appendingPathComponent("child"), state: state))
    }
}
