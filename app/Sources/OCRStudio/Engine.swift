import Foundation

/// Finds and launches the Python engine (`python -m ocrmd`).
enum Engine {
    static let supportDir: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("OCR Studio", isDirectory: true)
    }()
    static var venvPython: URL { supportDir.appendingPathComponent("venv/bin/python") }

    /// Directory that contains the `ocrmd` package.
    static func engineDir() -> URL? {
        let fm = FileManager.default
        if let env = ProcessInfo.processInfo.environment["OCRSTUDIO_ENGINE"] {
            return URL(fileURLWithPath: env)
        }
        if let res = Bundle.main.resourceURL?.appendingPathComponent("engine"),
           fm.fileExists(atPath: res.appendingPathComponent("ocrmd/__init__.py").path) {
            return res
        }
        // Development: walk up from the executable to the repository root.
        var dir = Bundle.main.executableURL?.deletingLastPathComponent()
        for _ in 0..<8 {
            guard let d = dir else { break }
            let cand = d.appendingPathComponent("engine")
            if fm.fileExists(atPath: cand.appendingPathComponent("ocrmd/__init__.py").path) { return cand }
            dir = d.deletingLastPathComponent()
        }
        return nil
    }

    static func python() -> URL? {
        let fm = FileManager.default
        if let env = ProcessInfo.processInfo.environment["OCRSTUDIO_PYTHON"] {
            return URL(fileURLWithPath: env)
        }
        // First interpreter that can really import the engine's packages (an empty venv is skipped).
        var candidates: [URL] = []
        if fm.isExecutableFile(atPath: venvPython.path) { candidates.append(venvPython) }
        if let dir = engineDir() {
            let dev = dir.deletingLastPathComponent().appendingPathComponent(".venv/bin/python")
            if fm.isExecutableFile(atPath: dev.path) { candidates.append(dev) }
        }
        candidates += systemPythons()
        return candidates.first(where: hasPackages)
    }

    private static var packageCache: [String: Bool] = [:]
    static func hasPackages(_ py: URL) -> Bool {
        if let c = packageCache[py.path] { return c }
        let p = Process()
        p.executableURL = py
        p.arguments = ["-c", "import pymupdf, PIL, numpy"]
        p.standardOutput = Pipe(); p.standardError = Pipe()
        let ok = (try? p.run()) != nil && { p.waitUntilExit(); return p.terminationStatus == 0 }()
        packageCache[py.path] = ok
        return ok
    }

    static func systemPythons() -> [URL] {
        [
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/opt/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3", "/usr/bin/python3",
        ].filter { FileManager.default.isExecutableFile(atPath: $0) }.map { URL(fileURLWithPath: $0) }
    }

    static func systemPython() -> URL? {
        let candidates = [
            "/opt/homebrew/bin/python3", "/usr/local/bin/python3",
            "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
            "/opt/local/bin/python3", "/usr/bin/python3",
        ]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }.map { URL(fileURLWithPath: $0) }
    }

    /// GUI apps get a minimal PATH, so add the usual package-manager locations.
    static func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let extra = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
        env["PATH"] = (extra + [(env["PATH"] ?? "/usr/bin:/bin")]).joined(separator: ":")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if let dir = engineDir() { env["PYTHONPATH"] = dir.path }
        return env
    }

    static func makeProcess(args: [String], extraEnv: [String: String] = [:]) -> Process? {
        guard let py = python(), let dir = engineDir() else { return nil }
        let p = Process()
        p.executableURL = py
        p.arguments = ["-m", "ocrmd"] + args
        p.environment = environment().merging(extraEnv) { _, new in new }
        p.currentDirectoryURL = dir
        return p
    }

    static func doctor() async -> Health? {
        guard let p = makeProcess(args: ["--doctor"]) else { return nil }
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        return await withCheckedContinuation { cont in
            DispatchQueue.global().async {
                do {
                    try p.run()
                    let data = pipe.fileHandleForReading.readDataToEndOfFile()
                    p.waitUntilExit()
                    cont.resume(returning: try? JSONDecoder().decode(Health.self, from: data))
                } catch {
                    cont.resume(returning: nil)
                }
            }
        }
    }

    /// Creates the private virtualenv and installs requirements.txt.
    static func setUp(log: @escaping @Sendable (String) -> Void) async throws {
        guard let dir = engineDir() else { throw SetupError("The engine folder is missing from this build.") }
        guard let sys = systemPython() else {
            throw SetupError("Python 3 was not found. Install it from python.org or with: brew install python")
        }
        let req = dir.deletingLastPathComponent().appendingPathComponent("requirements.txt")
        try FileManager.default.createDirectory(at: supportDir, withIntermediateDirectories: true)
        let venv = supportDir.appendingPathComponent("venv")
        log("Creating environment with \(sys.path)…")
        try await run(sys, ["-m", "venv", venv.path], log: log)
        log("Installing packages (this can take a few minutes)…")
        try await run(venvPython, ["-m", "pip", "install", "--upgrade", "pip"], log: log)
        try await run(venvPython, ["-m", "pip", "install", "-r", req.path], log: log)
        log("Done.")
    }

    private static func run(_ exe: URL, _ args: [String], log: @escaping @Sendable (String) -> Void) async throws {
        let p = Process()
        p.executableURL = exe
        p.arguments = args
        p.environment = environment()
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { h in
            let s = String(decoding: h.availableData, as: UTF8.self)
            for line in s.split(whereSeparator: \.isNewline) { log(String(line)) }
        }
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            p.terminationHandler = { proc in
                pipe.fileHandleForReading.readabilityHandler = nil
                if proc.terminationStatus == 0 { cont.resume() }
                else { cont.resume(throwing: SetupError("\(exe.lastPathComponent) exited with status \(proc.terminationStatus)")) }
            }
            do { try p.run() } catch { cont.resume(throwing: error) }
        }
    }
}

struct SetupError: LocalizedError {
    let message: String
    init(_ m: String) { message = m }
    var errorDescription: String? { message }
}

/// A running conversion; emits one callback per JSON line.
final class EngineRun {
    private let process: Process
    private let outPipe = Pipe()
    private let errPipe = Pipe()
    private var buffer = Data()
    private let lock = NSLock()
    private(set) var stderrTail: [String] = []

    init?(args: [String], extraEnv: [String: String] = [:]) {
        guard let p = Engine.makeProcess(args: args, extraEnv: extraEnv) else { return nil }
        process = p
        p.standardOutput = outPipe
        p.standardError = errPipe
    }

    private func consume(_ chunk: Data, _ onEvent: (EngineEvent, Data) -> Void) {
        lock.lock()
        buffer.append(chunk)
        var lines: [Data] = []
        while let nl = buffer.firstIndex(of: 0x0A) {
            lines.append(buffer.subdata(in: buffer.startIndex..<nl))
            buffer.removeSubrange(buffer.startIndex...nl)
        }
        lock.unlock()
        for line in lines {
            if let ev = try? JSONDecoder().decode(EngineEvent.self, from: line) { onEvent(ev, line) }
        }
    }

    func start(onEvent: @escaping (EngineEvent, Data) -> Void, onExit: @escaping (Int32) -> Void) throws {
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let chunk = h.availableData
            if !chunk.isEmpty { self?.consume(chunk, onEvent) }
        }
        errPipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let s = String(decoding: h.availableData, as: UTF8.self)
            guard let self, !s.isEmpty else { return }
            self.lock.lock()
            self.stderrTail.append(contentsOf: s.split(whereSeparator: \.isNewline).map(String.init))
            self.stderrTail = Array(self.stderrTail.suffix(30))
            self.lock.unlock()
        }
        process.terminationHandler = { [weak self] p in
            guard let self else { return }
            self.outPipe.fileHandleForReading.readabilityHandler = nil
            self.errPipe.fileHandleForReading.readabilityHandler = nil
            // Drain whatever is still buffered in the pipes before reporting the exit.
            self.consume(self.outPipe.fileHandleForReading.readDataToEndOfFile(), onEvent)
            let rest = String(decoding: self.errPipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            self.lock.lock()
            self.stderrTail.append(contentsOf: rest.split(whereSeparator: \.isNewline).map(String.init))
            self.lock.unlock()
            onExit(p.terminationStatus)
        }
        try process.run()
    }

    func cancel() { if process.isRunning { process.terminate() } }
    var tail: String { lock.lock(); defer { lock.unlock() }; return stderrTail.suffix(6).joined(separator: "\n") }
}
