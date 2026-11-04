import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct ConvertSettings {
    var ocr: OCRMode
    var languages: String
    var dpi: Int
    var figures: Bool
    var tables: Bool
    var pageMarkers: Bool
    var deskew: Bool
    var autoDPI: Bool
    var workers: Int            // 0 = automatic
    var filesAtOnce: Int        // 0 = automatic
    var outputDir: URL
    // AI correction
    var aiEnabled: Bool
    var aiProvider: String      // "ollama" | "openrouter"
    var ollamaURL: String
    var ollamaModel: String
    var openRouterModel: String
    var aiWorkers: Int
    var aiScope: String         // "ocr" | "all"
    var aiSkipConfident: Bool

    /// ~/Documents/ocr
    static var defaultOutput: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("ocr", isDirectory: true)
    }

    var aiModel: String { aiProvider == "ollama" ? ollamaModel : openRouterModel }
    /// Whether AI correction can actually run with the current settings.
    var aiReady: Bool {
        guard !aiModel.trimmingCharacters(in: .whitespaces).isEmpty else { return false }
        return aiProvider == "ollama" || !(Keychain.read("openrouter") ?? "").isEmpty
    }

    var aiArguments: [String] {
        var a = ["--ai", aiProvider, "--ai-model", aiModel, "--ai-workers", String(aiWorkers),
                 "--ai-scope", aiScope]
        if aiProvider == "ollama" { a += ["--ai-url", ollamaURL] }
        if aiSkipConfident { a += ["--ai-skip-confident", "95"] }
        return a
    }

    static func load() -> ConvertSettings {
        let d = UserDefaults.standard
        let path = d.string(forKey: "outputPath") ?? ""
        return ConvertSettings(
            ocr: OCRMode(rawValue: d.string(forKey: "ocrMode") ?? "") ?? .auto,
            languages: d.string(forKey: "languages") ?? "eng",
            dpi: d.object(forKey: "dpi") as? Int ?? 300,
            figures: d.object(forKey: "figures") as? Bool ?? true,
            tables: d.object(forKey: "tables") as? Bool ?? true,
            pageMarkers: d.object(forKey: "pageMarkers") as? Bool ?? true,
            deskew: d.object(forKey: "deskew") as? Bool ?? true,
            autoDPI: d.object(forKey: "autoDPI") as? Bool ?? true,
            workers: d.object(forKey: "workers") as? Int ?? 0,
            filesAtOnce: d.object(forKey: "filesAtOnce") as? Int ?? 0,
            outputDir: path.isEmpty ? defaultOutput : URL(fileURLWithPath: path),
            aiEnabled: d.object(forKey: "aiEnabled") as? Bool ?? false,
            aiProvider: d.string(forKey: "aiProvider") ?? "ollama",
            ollamaURL: d.string(forKey: "ollamaURL") ?? "http://localhost:11434",
            ollamaModel: d.string(forKey: "ollamaModel") ?? "",
            openRouterModel: d.string(forKey: "openRouterModel") ?? "google/gemini-2.0-flash-001",
            aiWorkers: d.object(forKey: "aiWorkers") as? Int ?? 3,
            aiScope: d.string(forKey: "aiScope") ?? "ocr",
            aiSkipConfident: d.object(forKey: "aiSkipConfident") as? Bool ?? true
        )
    }

    var arguments: [String] {
        var a = ["--json", "--preview", "--ocr", ocr.rawValue, "--lang", languages, "--dpi", String(dpi)]
        if !figures { a.append("--no-figures") }
        if !tables { a.append("--no-tables") }
        if !pageMarkers { a.append("--no-page-markers") }
        if !deskew { a.append("--no-deskew") }
        if !autoDPI { a.append("--no-auto-dpi") }
        if workers > 0 { a += ["--workers", String(workers)] }
        return a
    }
}

@MainActor
final class JobStore: ObservableObject {
    static let shared = JobStore()
    @Published var jobs: [Job] = []
    @Published var selection: UUID?
    @Published var health: Health?
    @Published var healthChecked = false
    @Published var showSetup = false
    @Published var setupLog: [String] = []
    @Published var isSettingUp = false
    @Published var setupError: String?

    private var runs: [UUID: EngineRun] = [:]
    /// Conversion only runs after the user presses Convert, so page ranges can be set first.
    @Published var started = false
    var hasQueued: Bool { jobs.contains { $0.status == .queued } }

    var selectedJob: Job? { jobs.first { $0.id == selection } }
    var engineAvailable: Bool { Engine.python() != nil && Engine.engineDir() != nil }
    var isBusy: Bool { jobs.contains { $0.status == .running } }

    /// How many files may convert at the same time. While one file waits on the AI (network), another can be read.
    private func concurrency(_ s: ConvertSettings) -> Int {
        if s.filesAtOnce > 0 { return s.filesAtOnce }
        return s.aiEnabled ? 2 : 1
    }

    init() { Task { await refreshHealth() } }

    // MARK: Health / setup
    func refreshHealth() async {
        health = await Engine.doctor()
        healthChecked = true
    }

    func setUp() {
        guard !isSettingUp else { return }
        isSettingUp = true
        setupError = nil
        setupLog = []
        Task {
            do {
                try await Engine.setUp { line in
                    Task { @MainActor in self.setupLog.append(line); self.setupLog = Array(self.setupLog.suffix(200)) }
                }
            } catch {
                setupError = error.localizedDescription
            }
            isSettingUp = false
            await refreshHealth()
        }
    }

    // MARK: Adding files
    func pickFiles() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = true
        panel.message = "Choose PDFs, scans or documents to convert to Markdown"
        if panel.runModal() == .OK { add(panel.urls) }
    }

    func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        var found = false
        for p in providers where p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            found = true
            p.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                var url: URL?
                if let data = item as? Data { url = URL(dataRepresentation: data, relativeTo: nil) }
                else if let u = item as? URL { url = u }
                if let url { Task { @MainActor in self.add([url]) } }
            }
        }
        return found
    }

    func add(_ urls: [URL]) {
        var files: [URL] = []
        for u in urls {
            var isDir: ObjCBool = false
            FileManager.default.fileExists(atPath: u.path, isDirectory: &isDir)
            if isDir.boolValue {
                let e = FileManager.default.enumerator(at: u, includingPropertiesForKeys: nil,
                                                       options: [.skipsHiddenFiles])
                for case let f as URL in e ?? NSEnumerator() where supportedExtensions.contains(f.pathExtension.lowercased()) {
                    files.append(f)
                }
            } else if supportedExtensions.contains(u.pathExtension.lowercased()) {
                files.append(u)
            }
        }
        files.sort { $0.path.localizedStandardCompare($1.path) == .orderedAscending }
        let existing = Set(jobs.filter { !$0.isFinished }.map(\.url))
        let st = ConvertSettings.load()
        for f in files where !existing.contains(f) {
            var j = Job(url: f)
            j.useAI = st.aiEnabled && st.aiReady
            jobs.append(j)
        }
        if selection == nil { selection = jobs.first?.id }
        if started { pump() }
    }

    func start() { started = true; pump() }

    func applyRange(_ range: String, toAllQueued: Bool = true) {
        for i in jobs.indices where jobs[i].status == .queued && jobs[i].isPDF { jobs[i].pageRange = range }
    }

    func setUseAI(_ id: UUID, _ on: Bool) {
        if let i = jobs.firstIndex(where: { $0.id == id }) { jobs[i].useAI = on }
    }

    func setRange(_ id: UUID, _ range: String) {
        if let i = jobs.firstIndex(where: { $0.id == id }) { jobs[i].pageRange = range }
    }

    func remove(_ id: UUID) {
        guard let j = jobs.first(where: { $0.id == id }) else { return }
        if j.status == .running { runs[id]?.cancel() }
        jobs.removeAll { $0.id == id }
        if selection == id { selection = jobs.first?.id }
    }

    func clearFinished() {
        jobs.removeAll { $0.isFinished }
        if selectedJob == nil { selection = jobs.first?.id }
    }

    func stop() {
        started = false
        for i in jobs.indices where jobs[i].status == .queued { jobs[i].status = .cancelled }
        runs.values.forEach { $0.cancel() }
    }

    func retry(_ id: UUID) {
        guard let i = jobs.firstIndex(where: { $0.id == id }) else { return }
        jobs[i].status = .queued; jobs[i].done = 0; jobs[i].total = 0; jobs[i].report = nil; jobs[i].log = []
        start()
    }

    // MARK: Running
    private func update(_ id: UUID, _ change: (inout Job) -> Void) {
        if let i = jobs.firstIndex(where: { $0.id == id }) { change(&jobs[i]) }
    }

    private func pump() {
        guard started else { return }
        let settings = ConvertSettings.load()
        while runs.count < concurrency(settings), let next = jobs.first(where: { $0.status == .queued }) {
            launch(next, settings)
        }
        if !hasQueued && runs.isEmpty { started = false }
    }

    private func launch(_ job: Job, _ settings: ConvertSettings) {
        try? FileManager.default.createDirectory(at: settings.outputDir, withIntermediateDirectories: true)
        let id = job.id
        update(id) { $0.status = .running; $0.phase = .reading }

        var args = [job.url.path, "-o", settings.outputDir.path] + settings.arguments
        if job.isPDF, let range = PageRange.parse(job.pageRange, max: job.pageCount), !range.isEmpty {
            args += ["--pages", range]
        }
        var env: [String: String] = [:]
        if job.useAI && settings.aiReady {
            args += settings.aiArguments
            if settings.aiProvider == "openrouter" { env["OCRMD_AI_KEY"] = Keychain.read("openrouter") ?? "" }
            update(id) { $0.aiModel = settings.aiModel; $0.aiProvider = settings.aiProvider }
        }
        guard let engineRun = EngineRun(args: args, extraEnv: env) else {
            update(id) { $0.status = .failed("The conversion engine isn't installed yet.") }
            showSetup = true
            return
        }
        runs[id] = engineRun
        do {
            try engineRun.start(onEvent: { [weak self] ev, raw in
                Task { @MainActor in self?.enqueue(ev, raw: raw, id: id) }
            }, onExit: { [weak self] code in
                Task { @MainActor in self?.finished(id: id, code: code, tail: engineRun.tail) }
            })
        } catch {
            runs[id] = nil
            update(id) { $0.status = .failed(error.localizedDescription) }
            pump()
        }
    }

    /// Engine events can arrive dozens per second (several pages finish at once); applying each one
    /// separately makes SwiftUI re-lay-out the whole live view every time. Batch them instead.
    private var pending: [(UUID, EngineEvent, Data)] = []
    private var flushScheduled = false

    private func enqueue(_ ev: EngineEvent, raw: Data, id: UUID) {
        pending.append((id, ev, raw))
        guard !flushScheduled else { return }
        flushScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in self?.flush() }
    }

    private func flush() {
        flushScheduled = false
        let batch = pending
        pending = []
        var jobsCopy = jobs                      // one @Published assignment per batch, not one per event
        swap(&jobsCopy, &jobs)
        for (id, ev, raw) in batch { handle(ev, raw: raw, id: id) }
    }

    private func handle(_ ev: EngineEvent, raw: Data, id: UUID) {
        switch ev.event {
        case "file":
            update(id) { $0.outDir = ev.outDir; $0.startedAt = Date() }
        case "start":
            update(id) {
                $0.total = ev.total ?? 0; $0.ocrPages = ev.ocrPageCount ?? 0; $0.lang = ev.lang ?? ""
                $0.workers = ev.workers ?? 0
                $0.pages = ev.pages ?? []
                $0.pageState = Dictionary(uniqueKeysWithValues: $0.pages.map { ($0, PageState.pending) })
                $0.results = [:]
            }
        case "page_start":
            if let p = ev.page { update(id) { $0.pageState[p] = .running } }
        case "page":
            withAnimation(.spring(response: 0.5, dampingFraction: 0.82)) {
                update(id) {
                    $0.done = ev.done ?? $0.done; $0.total = ev.total ?? $0.total
                    if let p = ev.page {
                        $0.pageState[p] = .done
                        $0.results[p] = PageResult(markdown: ev.md ?? "", kind: ev.kind ?? "text",
                                                   confidence: ev.conf, figures: ev.figures ?? [])
                    }
                }
            }
        case "ai_start":
            update(id) {
                $0.phase = .refining; $0.aiTotal = ev.total ?? 0; $0.aiDone = 0; $0.aiStartedAt = Date()
                $0.aiWorkers = ev.workers ?? 0; $0.aiModel = ev.model ?? $0.aiModel; $0.aiProvider = ev.provider ?? $0.aiProvider
                $0.aiState = [:]
            }
        case "ai_page_start":
            if let p = ev.page { update(id) { $0.aiState[p] = .running } }
        case "ai_page":
            withAnimation(.spring(response: 0.5, dampingFraction: 0.85)) {
                update(id) {
                    guard let p = ev.page else { return }
                    switch ev.status ?? "" {
                    case "corrected": $0.aiState[p] = .corrected(ev.changed ?? 0)
                    case "unchanged": $0.aiState[p] = .unchanged
                    case "skipped": $0.aiState[p] = .skipped(ev.reason ?? "")
                    default: $0.aiState[p] = .failed(ev.reason ?? "")
                    }
                    if let md = ev.md, var r = $0.results[p], ev.status != "failed" { r.markdown = md; $0.results[p] = r }
                    $0.aiKept[p] = ev.kept ?? 0
                    $0.aiDone = max($0.aiDone, ev.done ?? 0)
                }
            }
        case "warning":
            update(id) { $0.log.append(ev.message ?? "") }
        case "error":
            update(id) { $0.status = .failed(ev.message ?? "Conversion failed.") }
            if ev.code == "ocr_unavailable" { showSetup = true }
        case "done":
            let decoder = JSONDecoder()
            if let report = try? decoder.decode(Report.self, from: raw) {
                update(id) { $0.report = report; $0.status = .done; $0.done = $0.total }
            }
        default: break
        }
    }

    private func finished(id: UUID, code: Int32, tail: String) {
        update(id) { job in
            if case .running = job.status {
                job.status = (code == 15 || code == 143 || code == -15) ? .cancelled
                    : .failed(tail.isEmpty ? "The engine stopped unexpectedly (status \(code))." : tail)
            }
        }
        runs[id] = nil
        pump()
    }

    // MARK: Actions on results
    func reveal(_ job: Job) {
        guard let r = job.report else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: r.markdown)])
    }

    func copyMarkdown(_ job: Job) {
        guard let r = job.report, let s = try? String(contentsOfFile: r.markdown, encoding: .utf8) else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }

    func openInEditor(_ job: Job) {
        guard let r = job.report else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: r.markdown))
    }
}
