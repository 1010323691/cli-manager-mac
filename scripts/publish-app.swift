import Darwin
import Foundation

enum PublicationError: Error {
    case invalidArguments
    case unexpectedTarget
    case changedTarget
}

struct DirectoryIdentity: Equatable {
    let device: dev_t
    let inode: ino_t

    init?(_ path: String) {
        var metadata = stat()
        guard lstat(path, &metadata) == 0, metadata.st_mode & S_IFMT == S_IFDIR else { return nil }
        device = metadata.st_dev
        inode = metadata.st_ino
    }
}

func verifiedInstalledBundle(_ path: String, expectedID: String) throws -> DirectoryIdentity {
    let url = URL(fileURLWithPath: path, isDirectory: true)
    guard let identity = DirectoryIdentity(path), let app = Bundle(url: url),
          app.bundleIdentifier == expectedID,
          app.object(forInfoDictionaryKey: "CFBundleExecutable") as? String == "CLIManager" else {
        throw PublicationError.unexpectedTarget
    }
    let verifier = Process()
    verifier.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
    verifier.arguments = ["--verify", "--strict", path]
    verifier.standardOutput = FileHandle.nullDevice
    verifier.standardError = FileHandle.nullDevice
    try verifier.run()
    verifier.waitUntilExit()
    guard verifier.terminationStatus == 0, DirectoryIdentity(path) == identity else {
        throw PublicationError.unexpectedTarget
    }
    return identity
}

func publish() throws {
    let arguments = CommandLine.arguments
    guard arguments.count == 4, ["exclusive", "replace"].contains(arguments[1]) else {
        throw PublicationError.invalidArguments
    }
    let source = arguments[2]
    let destination = arguments[3]
    if arguments[1] == "exclusive" {
        guard renamex_np(source, destination, UInt32(RENAME_EXCL)) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        return
    }

    let directory = (destination as NSString).deletingLastPathComponent
    let lock = open(directory, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    guard lock >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    defer { close(lock) }
    guard flock(lock, LOCK_EX) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }

    if renamex_np(source, destination, UInt32(RENAME_EXCL)) == 0 { return }
    let failure = errno
    guard failure == EEXIST else { throw POSIXError(POSIXErrorCode(rawValue: failure) ?? .EIO) }
    guard let expectedID = Bundle(url: URL(fileURLWithPath: source, isDirectory: true))?.bundleIdentifier else {
        throw PublicationError.unexpectedTarget
    }
    let previous = try verifiedInstalledBundle(destination, expectedID: expectedID)
    guard renamex_np(source, destination, UInt32(RENAME_SWAP)) == 0 else {
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
    guard DirectoryIdentity(source) == previous else { throw PublicationError.changedTarget }
    try FileManager.default.removeItem(atPath: source)
}

do {
    try publish()
} catch {
    FileHandle.standardError.write(Data("CLI Manager bundle publication failed: \(error)\n".utf8))
    exit(1)
}
