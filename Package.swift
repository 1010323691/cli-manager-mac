// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "CLIManager",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "CLIManager", targets: ["CLIManager"])],
    dependencies: [.package(url: "https://github.com/migueldeicaza/SwiftTerm.git", revision: "139a0e853958a5a6a2da8e2962b489fff6eb3201")],
    targets: [
        .executableTarget(name: "CLIManager", dependencies: [.product(name: "SwiftTerm", package: "SwiftTerm")]),
        .testTarget(name: "CLIManagerTests", dependencies: ["CLIManager"], path: "tests/CLIManagerTests"),
    ]
)
