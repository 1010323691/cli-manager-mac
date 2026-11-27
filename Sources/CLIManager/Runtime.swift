import AppKit
import Darwin
import Foundation

enum CLIError: LocalizedError {
    case configuration(String)
    case command(String)
    case timeout
    case terminal

    var errorDescription: String? {
        switch self {
        case let .configuration(message), let .command(message): message
        case .timeout: "CLI 操作超时，请检查实例状态后重试。"
        case .terminal: "无法打开终端。"
        }
    }
}

struct Runtime: Sendable {
    let python: URL
    let bridge: URL
    let environment: [String: String]

    static func discover() throws -> Runtime {
        var environment = ProcessInfo.processInfo.environment
        reapAbandonedReceipts()
        let home = FileManager.default.homeDirectoryForCurrentUser
        let managers = [home.appendingPathComponent(".local/bin").path, "/opt/homebrew/bin", "/usr/local/bin"]
        let inherited = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        let paths = managers + inherited.split(separator: ":").map(String.init).filter { $0.hasPrefix("/") }
        environment["PATH"] = paths.joined(separator: ":")
        let managerURL = environment["CLI_MANAGER_BRIDGE"].map { URL(fileURLWithPath: $0) }
            ?? Bundle.main.resourceURL?.appendingPathComponent("desktop.py")
        guard let bridge = managerURL, FileManager.default.isReadableFile(atPath: bridge.path) else {
            throw CLIError.configuration("无法找到随应用安装的 CLI 接口。请从构建的 .app 启动。")
        }

        let configured = environment["CLI_MANAGER_PYTHON"]
        let candidates = configured.map { [$0] } ?? paths.flatMap { path in
            ["python3", "python3.14", "python3.13", "python3.12", "python3.11"].map { path + "/" + $0 }
        }
        for candidate in NSOrderedSet(array: candidates).compactMap({ $0 as? String }) {
            guard candidate.hasPrefix("/"), FileManager.default.isExecutableFile(atPath: candidate) else { continue }
            let python = URL(fileURLWithPath: candidate)
            let check = try? execute(python, ["-I", "-c", "import sys; sys.exit(sys.version_info < (3, 11))"],
                                     environment: environment, timeout: 3)
            if check?.status == 0 { return Runtime(python: python, bridge: bridge, environment: environment) }
        }
        throw CLIError.configuration("找不到 Python 3.11 或更高版本。")
    }

    func run<Response: Decodable>(_ arguments: [String], timeout: TimeInterval = 12) async throws -> Response {
        try await Task.detached(priority: .userInitiated) {
            let result = try Self.execute(python, ["-I", "-B", bridge.path] + arguments,
                                          environment: environment, timeout: timeout)
            let decoder = JSONDecoder()
            if result.status != 0 {
                let message = try? decoder.decode(Failure.self, from: result.data)
                throw CLIError.command(message?.error ?? "CLI 操作失败（退出码 \(result.status)）。")
            }
            do {
                return try decoder.decode(Response.self, from: result.data)
            } catch {
                throw CLIError.command("CLI 返回了无法识别的数据。")
            }
        }.value
    }

    struct ForegroundLaunch {
        let executable: String
        let arguments: [String]
        let environment: [String]
    }

    func userTerminalLaunch(_ instance: InstanceRecord, directory: URL) -> ForegroundLaunch {
        let keys = ["PATH", "LANG", "LANGUAGE", "LC_CTYPE", "LC_ALL", "TZ",
                    "CLI_MANAGER_STATE", "CLI_DELEGATE_V2_STATE", "CLI_DELEGATE_DEPTH"]
        let exports = keys.compactMap { key in environment[key].map { "\(key)=\($0)" } }
        return ForegroundLaunch(executable: python.path,
                                arguments: ["-I", "-B", bridge.path, "terminal", instance.id,
                                            "--expect", instance.guardToken, "--directory", directory.path],
                                environment: exports + ["TERM=xterm-256color"])
    }

    @MainActor
    func authenticate(_ action: String, instance: InstanceRecord) async throws {
        guard let terminal = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.Terminal") else {
            throw CLIError.terminal
        }
        let fm = FileManager.default
        let directory = fm.temporaryDirectory.appendingPathComponent("cli-manager-auth-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        let script = directory.appendingPathComponent("CLI Manager.command")
        let receipt = directory.appendingPathComponent("result")
        let awaiting = directory.appendingPathComponent("awaiting")
        var didLaunch = false
        defer {
            if !didLaunch || fm.fileExists(atPath: receipt.path) { try? fm.removeItem(at: directory) }
            else { try? fm.removeItem(at: awaiting) }
        }
        try Data().write(to: awaiting, options: .withoutOverwriting)

        let keys = ["PATH", "CLI_MANAGER_STATE", "CLI_DELEGATE_V2_STATE"]
        let exports = keys.map { key in
            environment[key].map { "export \(key)=\(Self.quote($0))" } ?? "unset \(key)"
        }.joined(separator: "\n")
        let arguments = [python.path, "-I", "-B", bridge.path, action, instance.id, "--expect", instance.guardToken]
        let line = arguments.map(Self.quote).joined(separator: " ")
        let body = """
        #!/bin/sh
        umask 077
        \(exports)
        trap 'exit 130' HUP INT TERM
        cleanup() {
          status=$?
          /bin/rm -f \(Self.quote(script.path))
          if [ -f \(Self.quote(awaiting.path)) ] && /bin/kill -0 \(ProcessInfo.processInfo.processIdentifier) 2>/dev/null; then
            /usr/bin/printf '%s\\n' "$status" > \(Self.quote(receipt.path + ".tmp"))
            /bin/mv -f \(Self.quote(receipt.path + ".tmp")) \(Self.quote(receipt.path))
          else
            /bin/rm -f \(Self.quote(awaiting.path))
            /bin/rmdir \(Self.quote(directory.path)) 2>/dev/null || :
          fi
        }
        trap cleanup EXIT
        /usr/bin/printf '\\nCLI Manager · %s · %s\\n\\n' \(Self.quote(instance.id)) \(Self.quote(action))
        \(line)
        """
        try Data(body.utf8).write(to: script, options: .withoutOverwriting)
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script.path)
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = true
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
            NSWorkspace.shared.open([script], withApplicationAt: terminal, configuration: configuration) { app, error in
                if let error { continuation.resume(throwing: error) }
                else if app == nil { continuation.resume(throwing: CLIError.terminal) }
                else { continuation.resume() }
            }
        }
        didLaunch = true
        let deadline = Date().addingTimeInterval(600)
        while Date() < deadline {
            if let data = try? String(contentsOf: receipt, encoding: .utf8),
               let status = Int(data.trimmingCharacters(in: .whitespacesAndNewlines)) {
                guard status == 0 else {
                    throw CLIError.command("「\(instance.id)」的账户操作未完成。请在终端查看结果。")
                }
                return
            }
            try await Task.sleep(for: .milliseconds(500))
        }
        throw CLIError.command("「\(instance.id)」仍未返回认证结果。请检查终端。")
    }

    private static func quote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private static func reapAbandonedReceipts() {
        let fm = FileManager.default
        let keys: [URLResourceKey] = [.creationDateKey, .isDirectoryKey, .isSymbolicLinkKey]
        guard let candidates = try? fm.contentsOfDirectory(at: fm.temporaryDirectory,
                                                           includingPropertiesForKeys: keys) else { return }
        let prefix = "cli-manager-auth-"
        let cutoff = Date().addingTimeInterval(-86_400)
        for directory in candidates where directory.lastPathComponent.hasPrefix(prefix) {
            let suffix = directory.lastPathComponent.dropFirst(prefix.count)
            guard UUID(uuidString: String(suffix)) != nil,
                  let properties = try? directory.resourceValues(forKeys: Set(keys)),
                  properties.isDirectory == true, properties.isSymbolicLink != true,
                  let created = properties.creationDate, created < cutoff,
                  let attributes = try? fm.attributesOfItem(atPath: directory.path),
                  (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
                  let files = try? fm.contentsOfDirectory(atPath: directory.path),
                  Set(files).isSubset(of: ["result", "result.tmp", "awaiting"]) else { continue }
            try? fm.removeItem(at: directory)
        }
    }

    private struct Failure: Decodable { var error: String }
    struct Result { var status: Int32; var data: Data }

    static func execute(
        _ binary: URL, _ args: [String], environment: [String: String], timeout: TimeInterval
    ) throws -> Result {
        let process = Process()
        let output = Pipe()
        let reader = output.fileHandleForReading
        let fd = reader.fileDescriptor
        guard fcntl(fd, F_SETFL, O_NONBLOCK) == 0 else {
            throw CLIError.command("无法打开 CLI 输出流。")
        }
        process.executableURL = binary
        process.arguments = args
        process.environment = environment
        process.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        try? output.fileHandleForWriting.close()
        defer {
            try? reader.close()
            if process.isRunning { stop(process) }
        }

        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 16_384)
        while ProcessInfo.processInfo.systemUptime < deadline {
            let count = buffer.withUnsafeMutableBytes { Darwin.read(fd, $0.baseAddress, $0.count) }
            if count > 0 {
                guard data.count + count <= 8 * 1_024 * 1_024 else {
                    throw CLIError.command("CLI 输出超过支持的大小。")
                }
                data.append(contentsOf: buffer.prefix(count))
                continue
            }
            if count < 0, errno != EAGAIN, errno != EWOULDBLOCK, errno != EINTR {
                throw CLIError.command("读取 CLI 输出失败。")
            }
            // A descendant can keep the pipe open after the command has exited.
            if !process.isRunning {
                return Result(status: process.terminationStatus, data: data)
            }
            usleep(10_000)
        }
        throw CLIError.timeout
    }

    private static func stop(_ process: Process) {
        process.terminate()
        let deadline = ProcessInfo.processInfo.systemUptime + 3
        while process.isRunning && ProcessInfo.processInfo.systemUptime < deadline { usleep(10_000) }
        if process.isRunning { _ = kill(process.processIdentifier, SIGKILL) }
    }
}
