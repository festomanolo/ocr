import SwiftUI

struct SettingsView: View {
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
    @AppStorage("outputPath") private var outputPath = ""

    var body: some View {
        Form {
            Section("Recognition") {
                Picker("Run OCR on", selection: $ocrMode) {
                    ForEach(OCRMode.allCases) { Text($0.title).tag($0.rawValue) }
                }
                Picker("Resolution", selection: $dpi) {
                    Text("200 dpi — fastest").tag(200)
                    Text("300 dpi — recommended").tag(300)
                    Text("400 dpi — small print").tag(400)
                }
                Toggle("Straighten tilted pages and even out lighting", isOn: $deskew)
                languagePicker
            }
            Section("Markdown") {
                Toggle("Extract figures as images", isOn: $figures)
                Toggle("Detect tables", isOn: $tables)
                Toggle("Add <!-- page N --> markers", isOn: $pageMarkers)
            }
            Section("Output") {
                HStack {
                    Text(outputPath.isEmpty ? ConvertSettings.defaultOutput.path : outputPath)
                        .lineLimit(1).truncationMode(.head).foregroundStyle(.secondary)
                    Spacer()
                    Button("Choose…") { chooseFolder() }
                }
            }
            Section("Engine") {
                LabeledContent("Tesseract", value: store.health?.tesseract ?? "not found")
                LabeledContent("MarkItDown", value: store.health?.markitdown == true ? "installed" : "not installed")
                Button("Set Up…") { store.showSetup = true }
            }
        }
        .formStyle(.grouped)
        .frame(width: 520, height: 560)
        .task { await store.refreshHealth() }
    }

    private var selected: Set<String> { Set(languages.split(separator: "+").map(String.init)) }

    private var languagePicker: some View {
        let installed = (store.health?.languages ?? ["eng"]).filter { $0 != "osd" }
        return DisclosureGroup("Languages: \(languages)") {
            if installed.isEmpty {
                Text("Install Tesseract to see languages.").foregroundStyle(.secondary)
            }
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
