// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "OCRStudio",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "OCRStudio",
            path: "Sources/OCRStudio"
        )
    ]
)
