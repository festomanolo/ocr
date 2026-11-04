import SwiftUI

struct ContentView: View {
    @EnvironmentObject var store: JobStore
    @State private var dropTargeted = false

    var body: some View {
        NavigationSplitView {
            SidebarView()
                .navigationSplitViewColumnWidth(min: 250, ideal: 290, max: 380)
        } detail: {
            Group {
                if let job = store.selectedJob {
                    JobDetailView(job: job)
                } else {
                    EmptyStateView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .toolbar {
            ToolbarItemGroup {
                Button { store.pickFiles() } label: { Label("Add Files", systemImage: "plus") }
                    .help("Add PDFs, scans or documents (⌘O)")
                if store.isBusy {
                    Button { store.stop() } label: { Label("Stop", systemImage: "stop.fill") }
                        .help("Stop the current conversion")
                }
            }
        }
        .onDrop(of: [.fileURL], isTargeted: $dropTargeted) { store.handleDrop($0) }
        .overlay { if dropTargeted { DropOverlay() } }
        .sheet(isPresented: $store.showSetup) { SetupView().environmentObject(store) }
    }
}

struct DropOverlay: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 18, style: .continuous)
            .strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 3, dash: [10, 7]))
            .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .overlay {
                VStack(spacing: 10) {
                    Image(systemName: "arrow.down.doc.fill").font(.system(size: 44))
                    Text("Drop to convert to Markdown").font(.title3.weight(.semibold))
                }
                .foregroundStyle(Color.accentColor)
            }
            .padding(14)
            .allowsHitTesting(false)
            .transition(.opacity)
    }
}

struct EmptyStateView: View {
    @EnvironmentObject var store: JobStore

    var body: some View {
        VStack(spacing: 22) {
            Image(systemName: "doc.text.viewfinder")
                .font(.system(size: 64, weight: .light))
                .foregroundStyle(.tint)
            VStack(spacing: 6) {
                Text("Turn PDFs into clean Markdown").font(.title2.weight(.semibold))
                Text("Drop text PDFs, scanned books or photos here.\nText, headings, tables and figures are extracted for you.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
            }
            Button { store.pickFiles() } label: {
                Label("Choose Files…", systemImage: "folder")
                    .padding(.horizontal, 8)
            }
            .controlSize(.large)
            .buttonStyle(.borderedProminent)

            if store.healthChecked { HealthCard().frame(maxWidth: 440) }
        }
        .padding(40)
    }
}

/// Shows what is missing (engine, Tesseract) with a one-click fix where possible.
struct HealthCard: View {
    @EnvironmentObject var store: JobStore

    var body: some View {
        let engine = store.engineAvailable && store.health != nil
        let tess = store.health?.tesseract != nil
        if engine && tess {
            let langs = (store.health?.languages ?? []).filter { $0 != "osd" }
            Label("Ready — Tesseract with \(langs.count) language\(langs.count == 1 ? "" : "s")",
                  systemImage: "checkmark.seal.fill")
                .foregroundStyle(.green)
                .font(.callout)
        } else {
            VStack(alignment: .leading, spacing: 10) {
                if !engine {
                    Label("The conversion engine needs a one-time setup.", systemImage: "wrench.and.screwdriver")
                    Button("Set Up…") { store.showSetup = true }
                } else {
                    Label("Tesseract isn't installed. Text PDFs still work; scanned pages need it.",
                          systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    CommandRow(command: "brew install tesseract tesseract-lang")
                }
            }
            .font(.callout)
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 12))
        }
    }
}

struct CommandRow: View {
    let command: String
    var body: some View {
        HStack {
            Text(command).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
            Spacer()
            Button { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(command, forType: .string) } label: {
                Image(systemName: "doc.on.doc")
            }
            .buttonStyle(.borderless).help("Copy")
        }
        .padding(8)
        .background(.background, in: RoundedRectangle(cornerRadius: 8))
    }
}

struct SetupView: View {
    @EnvironmentObject var store: JobStore
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Set up OCR Studio").font(.title2.weight(.semibold))
            VStack(alignment: .leading, spacing: 12) {
                requirement(ok: store.engineAvailable && store.health != nil,
                            title: "Conversion engine",
                            detail: "Python packages: PyMuPDF, MarkItDown, Pillow, NumPy. Installed privately for this app.")
                if !(store.engineAvailable && store.health != nil) {
                    HStack {
                        Button(store.isSettingUp ? "Installing…" : "Install Engine") { store.setUp() }
                            .disabled(store.isSettingUp)
                            .buttonStyle(.borderedProminent)
                        if store.isSettingUp { ProgressView().controlSize(.small) }
                    }
                }
                requirement(ok: store.health?.tesseract != nil,
                            title: "Tesseract OCR",
                            detail: "Reads scanned pages. Install with Homebrew in Terminal:")
                if store.health?.tesseract == nil {
                    CommandRow(command: "brew install tesseract tesseract-lang")
                    Button("Check Again") { Task { await store.refreshHealth() } }
                }
            }
            if let err = store.setupError {
                Text(err).foregroundStyle(.red).font(.callout)
            }
            if !store.setupLog.isEmpty {
                ScrollView {
                    Text(store.setupLog.joined(separator: "\n"))
                        .font(.system(size: 11, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(height: 140)
                .padding(8)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
            }
            HStack { Spacer(); Button("Done") { dismiss() }.keyboardShortcut(.defaultAction) }
        }
        .padding(24)
        .frame(width: 520)
    }

    private func requirement(ok: Bool, title: String, detail: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: ok ? "checkmark.circle.fill" : "circle.dashed")
                .foregroundStyle(ok ? .green : .secondary).font(.title3)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).fontWeight(.medium)
                Text(detail).font(.callout).foregroundStyle(.secondary)
            }
        }
    }
}
