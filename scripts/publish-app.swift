import Darwin
import Foundation

enum PublicationError: Error {
    case invalidArguments
    case unexpectedTarget
}

func publish() throws {
    let arguments = CommandLine.arguments
    guard arguments.count == 4, ["exclusive", "replace"].contains(arguments[1]) else {
        throw PublicationError.invalidArguments
    }
    let source = arguments[2]
    let destination = arguments[3]
    if renamex_np(source, destination, UInt32(RENAME_EXCL)) == 0 { return }
    let failure = errno
    guard failure == EEXIST, arguments[1] == "replace" else {
        throw POSIXError(POSIXErrorCode(rawValue: failure) ?? .EIO)
    }
    let path = URL(fileURLWithPath: destination, isDirectory: true)
    let attributes = try path.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
    guard attributes.isDirectory == true, attributes.isSymbolicLink != true else {
        throw PublicationError.unexpectedTarget
    }
    if renamex_np(source, destination, UInt32(RENAME_SWAP)) == 0 { return }
    throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
}

do {
    try publish()
} catch {
    FileHandle.standardError.write(Data("CLI Manager bundle publication failed: \(error)\n".utf8))
    exit(1)
}
