import SwiftUI
import WebKit

enum DetailTab: String, CaseIterable, Identifiable {
    case preview = "Preview", markdown = "Markdown", original = "Original OCR", figures = "Figures"
    var id: String { rawValue }
}

struct JobDetailView: View {
    @EnvironmentObject var store: JobStore
    let job: Job
    @State private var tab: DetailTab = .preview

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
        }
        .navigationTitle(job.name)
    }

    @ViewBuilder private var content: some View {
        switch job.status {
        case .queued:
            QueuedView(job: job)
        case .running:
            RunningView(job: job)
        case .cancelled:
            VStack(spacing: 12) {
                centered(icon: "xmark.circle", title: "Cancelled", detail: nil)
                Button("Convert Again") { store.retry(job.id) }
            }
        case .failed(let message):
            VStack(spacing: 12) {
                centered(icon: "exclamationmark.triangle.fill", title: "Couldn't convert this file", detail: message)
                Button("Try Again") { store.retry(job.id) }
            }
        case .done:
            if let r = job.report {
                switch tab {
                case .preview:
                    if r.preview?.isEmpty == false {
                        PreviewWebView(file: URL(fileURLWithPath: r.preview!), root: URL(fileURLWithPath: r.outDir))
                    } else {
                        MarkdownTextView(path: r.markdown)
                    }
                case .markdown: MarkdownTextView(path: r.markdown)
                case .original: MarkdownTextView(path: (r.markdown as NSString).deletingPathExtension + ".ocr.md")
                case .figures: FiguresView(dir: URL(fileURLWithPath: r.outDir).appendingPathComponent("figures"))
                }
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(job.name).font(.title3.weight(.semibold)).lineLimit(1).truncationMode(.middle)
                    Text(job.url.deletingLastPathComponent().path)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.head)
                }
                Spacer()
                if job.report != nil {
                    Button { store.copyMarkdown(job) } label: { Label("Copy", systemImage: "doc.on.doc") }
                    Button { store.reveal(job) } label: { Label("Show in Finder", systemImage: "folder") }
                    Button { store.openInEditor(job) } label: { Label("Open", systemImage: "arrow.up.forward.app") }
                }
            }
            if let r = job.report {
                HStack(alignment: .center, spacing: 12) {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            Chip(icon: "doc.plaintext", text: "\(r.pages) pages")
                            if r.textPages > 0 { Chip(icon: "textformat", text: "\(r.textPages) text layer") }
                            if r.ocrPages > 0 { Chip(icon: "eye", text: "\(r.ocrPages) OCR", tint: .purple) }
                            if r.figures > 0 { Chip(icon: "photo", text: "\(r.figures) figure\(r.figures == 1 ? "" : "s")") }
                            if r.tables > 0 { Chip(icon: "tablecells", text: "\(r.tables) table\(r.tables == 1 ? "" : "s")") }
                            if let c = r.avgConfidence {
                                Chip(icon: "checkmark.seal", text: "OCR \(Int(c))%", tint: c >= 80 ? .green : (c >= 60 ? .orange : .red))
                            }
                            Chip(icon: "text.word.spacing", text: "\(r.words.formatted()) words")
                            if let ai = r.ai {
                                Chip(icon: "sparkles", text: "AI · \(ai.corrected) corrected" + (ai.failed > 0 ? " · \(ai.failed) kept" : ""),
                                     tint: ai.failed > 0 ? .orange : .purple)
                            }
                        }
                    }
                    Picker("", selection: $tab) {
                        ForEach(DetailTab.allCases.filter { ($0 != .figures || r.figures > 0) && ($0 != .original || r.ai != nil) }) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.segmented).labelsHidden().frame(width: r.ai != nil ? 380 : 250)
                }
                if !r.lowConfidencePages.isEmpty {
                    Label("Low OCR confidence on page\(r.lowConfidencePages.count == 1 ? "" : "s") \(r.lowConfidencePages.prefix(12).map(String.init).joined(separator: ", ")). Check them against the original.",
                          systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.orange)
                }
            }
        }
        .padding(.horizontal, 20).padding(.vertical, 14)
    }

    private func centered(icon: String, title: String, detail: String?) -> some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 42, weight: .light)).foregroundStyle(.secondary)
            Text(title).font(.title3.weight(.medium))
            if let detail { Text(detail).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 420) }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }
}

struct Chip: View {
    let icon: String
    let text: String
    var tint: Color = .secondary
    var body: some View {
        Label(text, systemImage: icon)
            .font(.caption.weight(.medium))
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(tint.opacity(0.14), in: Capsule())
            .foregroundStyle(tint == .secondary ? Color.primary.opacity(0.75) : tint)
    }
}

struct RunningView: View {
    let job: Job
    var body: some View { LiveProgressView(job: job) }
}

// MARK: Rendered preview
struct PreviewWebView: NSViewRepresentable {
    let file: URL
    let root: URL

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let web = WKWebView(frame: .zero)
        web.setValue(false, forKey: "drawsBackground")
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {
        guard context.coordinator.loaded != file else { return }
        context.coordinator.loaded = file
        web.loadFileURL(file, allowingReadAccessTo: root)
    }

    final class Coordinator { var loaded: URL? }
}

// MARK: Raw markdown (NSTextView handles very large files far better than SwiftUI Text)
struct MarkdownTextView: NSViewRepresentable {
    let path: String

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        if let tv = scroll.documentView as? NSTextView {
            tv.isEditable = false
            tv.isRichText = false
            tv.font = .monospacedSystemFont(ofSize: 12.5, weight: .regular)
            tv.textContainerInset = NSSize(width: 18, height: 14)
            tv.drawsBackground = false
        }
        scroll.drawsBackground = false
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let tv = scroll.documentView as? NSTextView, context.coordinator.path != path else { return }
        context.coordinator.path = path
        let text = (try? String(contentsOfFile: path, encoding: .utf8)) ?? "Couldn't read the file."
        tv.string = text.count > 600_000 ? String(text.prefix(600_000)) + "\n\n… (truncated — open the file to see everything)" : text
        tv.scrollToBeginningOfDocument(nil)
    }

    func makeCoordinator() -> Coordinator { Coordinator() }
    final class Coordinator { var path = "" }
}

// MARK: Figures
struct FiguresView: View {
    let dir: URL
    @State private var files: [URL] = []

    var body: some View {
        ScrollView {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 190), spacing: 14)], spacing: 14) {
                ForEach(files, id: \.self) { url in
                    VStack(spacing: 6) {
                        Thumbnail(url: url)
                            .frame(height: 150)
                            .frame(maxWidth: .infinity)
                            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
                            .onTapGesture(count: 2) { NSWorkspace.shared.open(url) }
                        Text(url.lastPathComponent).font(.caption).foregroundStyle(.secondary)
                    }
                    .contextMenu {
                        Button("Open") { NSWorkspace.shared.open(url) }
                        Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([url]) }
                    }
                }
            }
            .padding(20)
        }
        .onAppear {
            files = ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
                .filter { ["png", "jpg", "jpeg"].contains($0.pathExtension.lowercased()) }
                .sorted { $0.lastPathComponent < $1.lastPathComponent }
        }
    }
}

struct Thumbnail: View {
    let url: URL
    @State private var image: NSImage?

    var body: some View {
        Group {
            if let image { Image(nsImage: image).resizable().scaledToFit().padding(6) }
            else { ProgressView().controlSize(.small) }
        }
        .task(id: url) {
            image = await Task.detached(priority: .utility) { () -> NSImage? in  // NSImage isn't Sendable before macOS 14
                guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
                let opts: [CFString: Any] = [
                    kCGImageSourceCreateThumbnailFromImageAlways: true,
                    kCGImageSourceThumbnailMaxPixelSize: 480,
                    kCGImageSourceCreateThumbnailWithTransform: true,
                ]
                guard let cg = CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary) else { return nil }
                return NSImage(cgImage: cg, size: .zero)
            }.value
        }
    }
}

/// Shown before conversion: choose which pages to read (handy for big scanned books).
struct QueuedView: View {
    @EnvironmentObject var store: JobStore
    let job: Job

    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: "doc.text.viewfinder").font(.system(size: 42, weight: .light)).foregroundStyle(.tint)
            Text("Ready to convert").font(.title3.weight(.medium))
            if job.isPDF {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Pages").font(.headline)
                    HStack {
                        TextField("All pages", text: Binding(get: { job.pageRange }, set: { store.setRange(job.id, $0) }))
                            .textFieldStyle(.roundedBorder).frame(width: 240)
                            .onSubmit { store.start() }
                        if let n = job.pageCount { Text("of \(n)").foregroundStyle(.secondary) }
                        if !job.pageRange.isEmpty {
                            Button("All") { store.setRange(job.id, "") }.buttonStyle(.borderless)
                        }
                    }
                    if job.rangeIsValid {
                        Text("Examples: 1-20   ·   5, 9, 12-15   ·   40-  (to the end). Leave empty for every page.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Label("Use numbers and ranges like 1-20, 45\(job.pageCount.map { " (1–\($0))" } ?? "")",
                              systemImage: "exclamationmark.triangle.fill")
                            .font(.caption).foregroundStyle(.red)
                    }
                    if store.jobs.filter({ $0.status == .queued && $0.isPDF }).count > 1, !job.pageRange.isEmpty, job.rangeIsValid {
                        Button("Use this range for all queued PDFs") { store.applyRange(job.pageRange) }
                            .buttonStyle(.link).font(.caption)
                    }
                }
                .frame(width: 400)
            }
            AIToggle(job: job)
            HStack {
                Button { store.start() } label: { Label("Convert", systemImage: "play.fill").padding(.horizontal, 10) }
                    .buttonStyle(.borderedProminent).controlSize(.large)
                    .disabled(!job.rangeIsValid)
                    .keyboardShortcut(.defaultAction)
                Button("Remove") { store.remove(job.id) }.controlSize(.large)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding()
    }
}
