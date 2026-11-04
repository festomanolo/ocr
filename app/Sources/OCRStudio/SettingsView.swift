import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var store: JobStore

    var body: some View {
        TabView {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }
            VisionSettings().tabItem { Label("Vision OCR", systemImage: "viewfinder") }
            AISettings().tabItem { Label("AI Correction", systemImage: "sparkles") }
            EngineSettings().tabItem { Label("Engine", systemImage: "cpu") }
        }
        .frame(width: 560, height: 600)
        .task { await store.refreshHealth() }
    }
}

// MARK: General

struct GeneralSettings: View {
    @EnvironmentObject var store: JobStore
    @AppStorage("ocrMode") private var ocrMode = OCRMode.auto.rawValue
    @AppStorage("languages") private var languages = "eng"
    @AppStorage("dpi") private var dpi = 300
    @AppStorage("figures") private var figures = true
    @AppStorage("tables") private var tables = true
    @AppStorage("pageMarkers") private var pageMarkers = true
    @AppStorage("deskew") private var deskew = true
    @AppStorage("autoDPI") private var autoDPI = true
    @AppStorage("workers") private var workers = 0
    @AppStorage("filesAtOnce") private var filesAtOnce = 0
    @AppStorage("outputPath") private var outputPath = ""

    var body: some View {
        Form {
            Section("Recognition") {
                Picker("Run OCR on", selection: $ocrMode) {
                    ForEach(OCRMode.allCases) { Text($0.title).tag($0.rawValue) }
                }
                Picker("Maximum resolution", selection: $dpi) {
                    Text("200 dpi — fastest").tag(200)
                    Text("300 dpi — recommended").tag(300)
                    Text("400 dpi — small print").tag(400)
                }
                Toggle("Match each scan's real resolution (faster)", isOn: $autoDPI)
                Toggle("Straighten tilted pages and even out lighting", isOn: $deskew)
                languagePicker
            }
            Section {
                Picker("Pages at once", selection: $workers) {
                    Text("Automatic (\(autoWorkers))").tag(0)
                    ForEach([1, 2, 3, 4, 6, 8], id: \.self) { Text("\($0)").tag($0) }
                }
                Picker("Files at once", selection: $filesAtOnce) {
                    Text("Automatic").tag(0)
                    ForEach([1, 2, 3], id: \.self) { Text("\($0)").tag($0) }
                }
            } header: { Text("Speed") } footer: {
                Text("Each page is read by its own Tesseract process. With AI correction on, two files run together so one reads pages while the other waits for the AI.")
            }
            Section("Markdown") {
                Toggle("Extract figures as images", isOn: $figures)
                Toggle("Detect tables", isOn: $tables)
                Toggle("Add <!-- page N --> markers", isOn: $pageMarkers)
            }
            Section("Output folder") {
                HStack {
                    Text(outputPath.isEmpty ? ConvertSettings.defaultOutput.path : outputPath)
                        .lineLimit(1).truncationMode(.head).foregroundStyle(.secondary)
                    Spacer()
                    Button("Choose…") { chooseFolder() }
                    if !outputPath.isEmpty { Button("Reset") { outputPath = "" } }
                }
            }
        }
        .formStyle(.grouped)
    }

    private var autoWorkers: Int { max(1, min(6, ProcessInfo.processInfo.activeProcessorCount / 2)) }
    private var selected: Set<String> { Set(languages.split(separator: "+").map(String.init)) }

    private var languagePicker: some View {
        let installed = (store.health?.languages ?? ["eng"]).filter { $0 != "osd" }
        return DisclosureGroup("Languages: \(languages)") {
            if installed.isEmpty { Text("Install Tesseract to see languages.").foregroundStyle(.secondary) }
            ForEach(installed, id: \.self) { code in
                Toggle(code, isOn: Binding(
                    get: { selected.contains(code) },
                    set: { on in
                        var s = selected
                        if on { s.insert(code) } else { s.remove(code) }
                        if s.isEmpty { s = ["eng"] }
                        languages = s.sorted().joined(separator: "+")
                    }))
            }
        }
    }

    private func chooseFolder() {
        let p = NSOpenPanel()
        p.canChooseFiles = false; p.canChooseDirectories = true; p.canCreateDirectories = true
        if p.runModal() == .OK, let u = p.url { outputPath = u.path }
    }
}

// MARK: Vision layer

struct VisionSettings: View {
    @AppStorage("visionEnabled") private var enabled = false
    @AppStorage("visionProvider") private var provider = "ollama"
    @AppStorage("visionModel") private var model = ConvertSettings.defaultVisionModel
    @AppStorage("visionURL") private var url = ""
    @AppStorage("visionMode") private var mode = "low-confidence"
    @AppStorage("visionMinConf") private var minConf = 85

    @State private var apiKey = Keychain.read("vision") ?? ""
    @State private var status: (text: String, ok: Bool)?
    @State private var checking = false

    var body: some View {
        Form {
            Section {
                Toggle("Re-read difficult pages with a vision model", isOn: $enabled)
            } footer: {
                Text("A second reading layer inspired by document-AI tools: Tesseract reads every page first (fast, keeps figures); pages it was unsure about are re-read by a vision model that looks at the whole page and returns Markdown with tables and formulas. Based on Xiaomi-OCR-0 (open source, Apache 2.0).")
            }
            if enabled {
                Section("Model") {
                    Picker("Runs on", selection: $provider) {
                        Text("Ollama — this Mac").tag("ollama")
                        Text("OpenAI-compatible server").tag("openai")
                    }
                    .pickerStyle(.segmented)
                    TextField("Model", text: $model).textFieldStyle(.roundedBorder)
                    TextField(provider == "ollama" ? "Address (default http://localhost:11434)"
                                                   : "Server address (default http://127.0.0.1:8000/v1)", text: $url)
                        .textFieldStyle(.roundedBorder)
                    if provider == "openai" {
                        SecureField("API key (optional)", text: $apiKey)
                            .textFieldStyle(.roundedBorder)
                            .onChange(of: apiKey) { Keychain.write("vision", $0.trimmingCharacters(in: .whitespacesAndNewlines)) }
                    }
                    HStack {
                        Button(checking ? "Checking…" : "Check Model") { Task { await check() } }.disabled(checking)
                        if provider == "ollama" { Spacer(); CommandRow(command: "ollama pull \(ConvertSettings.defaultVisionModel)") }
                    }
                    if let s = status {
                        Label(s.text, systemImage: s.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                            .foregroundStyle(s.ok ? .green : .orange).font(.callout)
                    }
                }
                Section {
                    Picker("Use it for", selection: $mode) {
                        Text("Pages Tesseract was unsure about").tag("low-confidence")
                        Text("Every scanned page").tag("all")
                    }
                    if mode == "low-confidence" {
                        Stepper("Re-read pages below \(minConf)% confidence", value: $minConf, in: 50...99, step: 5)
                    }
                } header: { Text("When") } footer: {
                    Text("The vision model is much slower than Tesseract. Use it for tables, formulas and messy layouts. It needs a capable machine: Apple Silicon or a GPU server (vLLM / SGLang) is recommended, and Ollama support for this model is still experimental. If it fails, the Tesseract text is kept.")
                }
            }
        }
        .formStyle(.grouped)
    }

    private func check() async {
        checking = true; defer { checking = false }
        if provider == "ollama" {
            switch await AIService.ollamaModels(baseURL: url.isEmpty ? "http://localhost:11434" : url) {
            case .success(let names):
                status = names.contains(model) ? ("\(model) is installed", true)
                                               : ("\(model) isn't installed yet — run the command above", false)
            case .failure(let e): status = (e.localizedDescription, false)
            }
        } else {
            let base = (url.isEmpty ? "http://127.0.0.1:8000/v1" : url).trimmingSuffix("/")
            guard let u = URL(string: base + "/models") else { status = ("Invalid address", false); return }
            var req = URLRequest(url: u); req.timeoutInterval = 5
            if !apiKey.isEmpty { req.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization") }
            do {
                let (_, resp) = try await URLSession.shared.data(for: req)
                let ok = (resp as? HTTPURLResponse)?.statusCode == 200
                status = (ok ? "Server is reachable" : "The server answered with an error", ok)
            } catch { status = ("Can't reach \(base)", false) }
        }
    }
}

// MARK: AI

struct AISettings: View {
    @AppStorage("aiEnabled") private var enabled = false
    @AppStorage("aiProvider") private var provider = "ollama"
    @AppStorage("ollamaURL") private var ollamaURL = "http://localhost:11434"
    @AppStorage("ollamaModel") private var ollamaModel = ""
    @AppStorage("openRouterModel") private var openRouterModel = "google/gemini-2.0-flash-001"
    @AppStorage("aiWorkers") private var aiWorkers = 3
    @AppStorage("aiScope") private var scope = "ocr"
    @AppStorage("aiSkipConfident") private var skipConfident = true

    @State private var apiKey = Keychain.read("openrouter") ?? ""
    @State private var models: [String] = []
    @State private var status: (text: String, ok: Bool)?
    @State private var checking = false

    var body: some View {
        Form {
            Section {
                Toggle("Correct recognised text with AI", isOn: $enabled)
            } footer: {
                Text("After a file is read, each page is checked against its neighbours and obvious OCR mistakes are fixed. The untouched OCR is saved next to it as “….ocr.md”. Figures and page order are never changed.")
            }

            if enabled {
                Section("Model") {
                    Picker("Provider", selection: $provider) {
                        Text("Ollama — on this Mac").tag("ollama")
                        Text("OpenRouter — cloud").tag("openrouter")
                    }
                    .pickerStyle(.segmented)

                    if provider == "ollama" { ollama } else { openRouter }
                    if let s = status {
                        Label(s.text, systemImage: s.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                            .foregroundStyle(s.ok ? .green : .orange).font(.callout)
                    }
                }

                Section {
                    Picker("Pages corrected at once", selection: $aiWorkers) {
                        ForEach([1, 2, 3, 4, 6, 8], id: \.self) { Text("\($0)").tag($0) }
                    }
                    Picker("Pages to correct", selection: $scope) {
                        Text("Scanned (OCR) pages only").tag("ocr")
                        Text("Every page").tag("all")
                    }
                    Toggle("Skip pages Tesseract was very sure about (≥ 95%)", isOn: $skipConfident)
                } header: { Text("Speed & scope") } footer: {
                    Text("Corrections run in parallel; each page sees the original text of the pages around it. Ollama usually handles 1–2 at once, OpenRouter 3–6.")
                }
            }
        }
        .formStyle(.grouped)
        .task { if provider == "ollama" { await refreshModels() } }
        .onChange(of: provider) { p in status = nil; if p == "ollama" { Task { await refreshModels() } } }
    }

    private var ollama: some View {
        Group {
            TextField("Address", text: $ollamaURL).textFieldStyle(.roundedBorder)
            HStack {
                Picker("Model", selection: $ollamaModel) {
                    if ollamaModel.isEmpty { Text("Choose a model").tag("") }
                    ForEach(models, id: \.self) { Text($0).tag($0) }
                    if !ollamaModel.isEmpty && !models.contains(ollamaModel) { Text(ollamaModel).tag(ollamaModel) }
                }
                Button { Task { await refreshModels() } } label: { Image(systemName: "arrow.clockwise") }
                    .help("Reload the model list").disabled(checking)
            }
            Text("Bigger models make fewer mistakes: 7B or larger (for example qwen2.5:7b) is recommended. 1–3B models can misjudge words.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var openRouter: some View {
        Group {
            SecureField("API key", text: $apiKey)
                .textFieldStyle(.roundedBorder)
                .onChange(of: apiKey) { Keychain.write("openrouter", $0.trimmingCharacters(in: .whitespacesAndNewlines)) }
            HStack {
                TextField("Model", text: $openRouterModel).textFieldStyle(.roundedBorder)
                Menu {
                    ForEach(AIService.openRouterSuggestions, id: \.self) { m in Button(m) { openRouterModel = m } }
                } label: { Image(systemName: "chevron.down.circle") }
                .menuStyle(.borderlessButton).fixedSize()
            }
            HStack {
                Button(checking ? "Checking…" : "Test Key") { Task { await testKey() } }.disabled(checking || apiKey.isEmpty)
                Link("Get a key", destination: URL(string: "https://openrouter.ai/keys")!).font(.callout)
            }
            Label("Page text (not images) is sent to OpenRouter and the model's provider. Keep this off for confidential documents.",
                  systemImage: "lock.open").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func refreshModels() async {
        checking = true; defer { checking = false }
        switch await AIService.ollamaModels(baseURL: ollamaURL) {
        case .success(let names):
            models = names
            if ollamaModel.isEmpty || !names.contains(ollamaModel) {
                // Prefer the largest-looking model that is installed.
                ollamaModel = names.sorted { rank($0) > rank($1) }.first ?? ""
            }
            status = names.isEmpty ? ("No models installed. Run: ollama pull qwen2.5:7b", false)
                                   : ("Ollama is running — \(names.count) model\(names.count == 1 ? "" : "s") available", true)
        case .failure(let e):
            status = (e.localizedDescription, false)
        }
    }

    private func rank(_ name: String) -> Double {
        // "qwen2.5:7b" → 7, "llama3:70b" → 70, "…:1.5b" → 1.5
        guard let tag = name.split(separator: ":").last?.lowercased(), tag.hasSuffix("b"),
              let n = Double(tag.dropLast()) else { return 0 }
        return n
    }

    private func testKey() async {
        checking = true; defer { checking = false }
        switch await AIService.testOpenRouter(key: apiKey) {
        case .success(let m): status = (m, true)
        case .failure(let e): status = (e.localizedDescription, false)
        }
    }
}

// MARK: Engine

struct EngineSettings: View {
    @EnvironmentObject var store: JobStore
    var body: some View {
        Form {
            Section("Components") {
                LabeledContent("Tesseract", value: store.health?.tesseract ?? "not found")
                LabeledContent("Languages", value: (store.health?.languages ?? []).filter { $0 != "osd" }.joined(separator: ", "))
                LabeledContent("MarkItDown", value: store.health?.markitdown == true ? "installed" : "not installed")
                Button("Set Up…") { store.showSetup = true }
            }
        }
        .formStyle(.grouped)
    }
}
