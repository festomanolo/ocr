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
    var outputDir: URL

    static var defaultOutput: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("OCR Studio", isDirectory: true)
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
            outputDir: path.isEmpty ? defaultOutput : URL(fileURLWithPath: path)
        )
    }

    var arguments: [String] {
        var a = ["--json", "--preview", "--ocr", ocr.rawValue, "--lang", languages, "--dpi", String(dpi)]
        if !figures { a.append("--no-figures") }
        if !tables { a.append("--no-tables") }
        if !pageMarkers { a.append("--no-page-markers") }
        if !deskew { a.append("--no-deskew") }
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

    private var run: EngineRun?
    private var pumping = false
    /// Conversion only runs after the user presses Convert, so page ranges can be set first.
    @Published var started = false
    var hasQueued: Bool { jobs.contains { $0.status == .queued } }

    var selectedJob: Job? { jobs.first { $0.id == selection } }
    var engineAvailable: Bool { Engine.python() != nil && Engine.engineDir() != nil }
    var isBusy: Bool { jobs.contains { $0.status == .running } }

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
        for f in files where !existing.contains(f) { jobs.append(Job(url: f)) }
        if selection == nil { selection = jobs.first?.id }
        if started { pump() }
    }

    func start() { started = true; pump() }

    func applyRange(_ range: String, toAllQueued: Bool = true) {
        for i in jobs.indices where jobs[i].status == .queued && jobs[i].isPDF { jobs[i].pageRange = range }
    }

    func setRange(_ id: UUID, _ range: String) {
        if let i = jobs.firstIndex(where: { $0.id == id }) { jobs[i].pageRange = range }
    }

    func remove(_ id: UUID) {
        guard let j = jobs.first(where: { $0.id == id }) else { return }
        if j.status == .running { run?.cancel() }
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
        run?.cancel()
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
        guard started, !pumping, let next = jobs.first(where: { $0.status == .queued }) else {
            if !hasQueued && !isBusy { started = false }
            return
        }
        pumping = true
        let settings = ConvertSettings.load()
        try? FileManager.default.createDirectory(at: settings.outputDir, withIntermediateDirectories: true)
        let id = next.id
        update(id) { $0.status = .running }

        var args = [next.url.path, "-o", settings.outputDir.path] + settings.arguments
        if next.isPDF, let range = PageRange.parse(next.pageRange, max: next.pageCount), !range.isEmpty {
            args += ["--pages", range]
        }
        guard let engineRun = EngineRun(args: args) else {
            update(id) { $0.status = .failed("The conversion engine isn't installed yet.") }
            pumping = false
            showSetup = true
            return
        }
        run = engineRun
        do {
            try engineRun.start(onEvent: { [weak self] ev, raw in
                Task { @MainActor in self?.handle(ev, raw: raw, id: id) }
            }, onExit: { [weak self] code in
                Task { @MainActor in self?.finished(id: id, code: code, tail: engineRun.tail) }
            })
        } catch {
            update(id) { $0.status = .failed(error.localizedDescription) }
            pumping = false
            pump()
        }
    }

    private func handle(_ ev: EngineEvent, raw: Data, id: UUID) {
        switch ev.event {
        case "start":
            update(id) { $0.total = ev.total ?? 0; $0.ocrPages = ev.ocrPageCount ?? 0; $0.lang = ev.lang ?? "" }
        case "page":
            update(id) { $0.done = ev.done ?? $0.done; $0.total = ev.total ?? $0.total }
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
        run = nil
        pumping = false
        pump()
        Task { await refreshHealth() }
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
