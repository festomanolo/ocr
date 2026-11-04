import PDFKit
import SwiftUI

// MARK: - Layout: progress header · pages being processed (left) · live output (right)

struct LiveProgressView: View {
    @EnvironmentObject var store: JobStore
    let job: Job
    @StateObject private var thumbs: Thumbnailer

    init(job: Job) {
        self.job = job
        _thumbs = StateObject(wrappedValue: Thumbnailer(url: job.url))
    }

    var body: some View {
        VStack(spacing: 0) {
            ProgressHeader(job: job)
            Divider()
            HStack(spacing: 0) {
                PagesPanel(job: job, thumbs: thumbs)
                    .frame(minWidth: 230, idealWidth: 300, maxWidth: 400)
                Divider()
                OutputPanel(job: job)
                    .frame(minWidth: 280, maxWidth: .infinity)
            }
        }
    }
}

// MARK: Header

struct ProgressHeader: View {
    @EnvironmentObject var store: JobStore
    let job: Job

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { ctx in
            let start = (job.phase == .refining ? job.aiStartedAt : job.startedAt) ?? job.startedAt
            let elapsed = start.map { ctx.date.timeIntervalSince($0) } ?? 0
            let eta = estimate(elapsed: elapsed)
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline) {
                    if job.phase == .refining {
                        Label("Refining with AI", systemImage: "sparkles").font(.title3.weight(.semibold)).foregroundStyle(.purple)
                        Text("page \(job.aiDone) of \(job.aiTotal)").font(.title3).foregroundStyle(.secondary).monospacedDigit()
                    } else {
                        Text(job.total == 0 ? "Opening document…" : "Converting").font(.title3.weight(.semibold))
                        Text(job.total > 0 ? "page \(job.done) of \(job.total)" : "")
                            .font(.title3).foregroundStyle(.secondary).monospacedDigit()
                    }
                    Spacer()
                    Button(role: .cancel) { store.stop() } label: { Label("Stop", systemImage: "stop.fill") }
                        .controlSize(.regular)
                }
                if job.useAI || !job.aiModel.isEmpty { PhaseSteps(job: job) }
                ProgressView(value: job.fraction)
                    .progressViewStyle(.linear)
                    .tint(job.phase == .refining ? .purple : .accentColor)
                    .animation(.easeOut(duration: 0.4), value: job.fraction)
                HStack(spacing: 16) {
                    Stat(icon: "percent", text: "\(Int(job.fraction * 100))%")
                    Stat(icon: "clock", text: format(elapsed) + " elapsed")
                    if let eta { Stat(icon: "hourglass", text: "about " + format(eta) + " left") }
                    if job.phase == .refining {
                        Stat(icon: "square.grid.2x2", text: "\(job.aiWorkers) page\(job.aiWorkers == 1 ? "" : "s") at once")
                        Stat(icon: "sparkles", text: job.aiModel)
                    } else {
                        if job.workers > 0 { Stat(icon: "square.grid.2x2", text: "\(job.workers) page\(job.workers == 1 ? "" : "s") at once") }
                        if job.ocrPages > 0 { Stat(icon: "eye", text: "Tesseract · \(job.lang)") }
                    }
                    Spacer()
                }
                .font(.caption)
                .transaction { $0.animation = nil }
            }
            .padding(.horizontal, 20).padding(.vertical, 14)
            .background(.bar)
        }
    }

    private func estimate(elapsed: TimeInterval) -> TimeInterval? {
        let done = job.phase == .refining ? job.aiDone : job.done
        let total = job.phase == .refining ? job.aiTotal : job.total
        guard done >= 2, total > done, elapsed > 3 else { return nil }
        return elapsed / Double(done) * Double(total - done)
    }

    private func format(_ t: TimeInterval) -> String {
        let s = Int(t.rounded())
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, (s / 60) % 60, s % 60)
                         : String(format: "%d:%02d", s / 60, s % 60)
    }
}

/// "① Read pages → ② Refine with AI" progress steps.
struct PhaseSteps: View {
    let job: Job
    var body: some View {
        HStack(spacing: 8) {
            step("Read pages", "doc.text.viewfinder", done: job.phase == .refining, active: job.phase == .reading)
            Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary)
            step("Refine with AI", "sparkles", done: false, active: job.phase == .refining)
            Spacer()
        }
        .font(.caption)
    }

    private func step(_ title: String, _ icon: String, done: Bool, active: Bool) -> some View {
        HStack(spacing: 5) {
            Image(systemName: done ? "checkmark.circle.fill" : icon)
            Text(title)
        }
        .padding(.horizontal, 9).padding(.vertical, 4)
        .background(active ? Color.accentColor.opacity(0.15) : Color.clear, in: Capsule())
        .foregroundStyle(done ? Color.green : (active ? Color.primary : Color.secondary))
        .animation(.easeInOut(duration: 0.3), value: active)
    }
}

struct Stat: View {
    let icon: String
    let text: String
    var body: some View {
        Label { Text(text).monospacedDigit() } icon: { Image(systemName: icon) }
            .foregroundStyle(.secondary)
    }
}

// MARK: Pages panel

struct PagesPanel: View {
    let job: Job
    @ObservedObject var thumbs: Thumbnailer
    @State private var follow = true

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Pages").font(.headline)
                Spacer()
                Toggle(isOn: $follow) { Text("Follow") }.toggleStyle(.switch).controlSize(.mini)
                    .help("Keep the page being read in view")
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 70, maximum: 100), spacing: 12)], spacing: 14) {
                        ForEach(job.pages, id: \.self) { n in
                            PageTile(number: n, state: job.pageState[n] ?? .pending,
                                     result: job.results[n], ai: job.aiState[n], thumbs: thumbs)
                                .id(n)
                        }
                    }
                    .padding(16)
                }
                .onChange(of: runningAnchor) { anchor in
                    guard follow, let anchor else { return }
                    proxy.scrollTo(anchor, anchor: .center)     // no animation: animated scrolls in a lazy grid thrash layout
                }
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    /// The lowest page currently being read.
    private var runningAnchor: Int? {
        job.pages.first { job.pageState[$0] == .running }
    }
}

struct PageTile: View {
    let number: Int
    let state: PageState
    let result: PageResult?
    var ai: AIState?
    @ObservedObject var thumbs: Thumbnailer
    @State private var image: NSImage?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var refining: Bool { ai == .running }
    private var active: Bool { state == .running || refining }
    private var accent: Color { refining ? .purple : .accentColor }

    var body: some View {
        VStack(spacing: 5) {
            ZStack {
                RoundedRectangle(cornerRadius: 7, style: .continuous).fill(.quaternary.opacity(0.5))
                if let image {
                    Image(nsImage: image).resizable().scaledToFit()
                        .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                        .padding(3)
                        .saturation(state == .pending ? 0 : 1)
                        .opacity(state == .pending ? 0.55 : 1)
                } else {
                    Image(systemName: "doc").foregroundStyle(.tertiary)
                }
                if active { scanBeam }
            }
            .aspectRatio(0.72, contentMode: .fit)
            .overlay {
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .strokeBorder(active ? accent : Color.primary.opacity(0.08), lineWidth: active ? 2 : 1)
            }
            .overlay(alignment: .bottomTrailing) { badge }
            .shadow(color: active ? accent.opacity(0.35) : .clear, radius: 8, y: 2)
            .scaleEffect(active ? 1.05 : 1)
            .animation(.spring(response: 0.45, dampingFraction: 0.7), value: state)
            .animation(.spring(response: 0.45, dampingFraction: 0.7), value: ai)

            HStack(spacing: 3) {
                Text("\(number)").monospacedDigit()
                if let r = result, r.kind == "ocr" { Image(systemName: "eye").font(.system(size: 8)) }
            }
            .font(.caption2.weight(active ? .semibold : .regular))
            .foregroundStyle(active ? accent : .secondary)
        }
        .task { image = await thumbs.image(for: number) }
    }

    /// Green check once read; purple sparkle once the AI has corrected the page; orange warning if the AI kept the original.
    @ViewBuilder private var badge: some View {
        let symbol: (String, Color)? = {
            switch ai {
            case .corrected: return ("sparkles", .purple)
            case .failed: return ("exclamationmark.triangle.fill", .orange)
            default: return state == .done ? ("checkmark.circle.fill", .green) : nil
            }
        }()
        if let (name, color) = symbol, !refining {
            Image(systemName: name)
                .symbolRenderingMode(name == "checkmark.circle.fill" ? .palette : .monochrome)
                .foregroundStyle(name == "checkmark.circle.fill" ? Color.white : color, color)
                .font(.system(size: 17))
                .background(Circle().fill(.background).padding(2))
                .offset(x: 5, y: 5)
                .transition(.scale(scale: 0.2).combined(with: .opacity))
        }
    }

    /// A soft scan line sweeping over the page that is being read.
    @ViewBuilder private var scanBeam: some View {
        if reduceMotion {
            Rectangle().fill(accent.opacity(0.12))
        } else {
            GeometryReader { g in
                LinearGradient(colors: [.clear, accent.opacity(0.55), .clear],
                               startPoint: .top, endPoint: .bottom)
                    .frame(height: 26)
                    .offset(y: (g.size.height - 26) * beam)
            }
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            .allowsHitTesting(false)
        }
    }
}

// MARK: Live output

struct OutputPanel: View {
    let job: Job
    @State private var follow = true

    private var donePages: [Int] { job.pages.filter { job.results[$0] != nil } }
    private var running: [Int] {
        job.phase == .refining ? job.pages.filter { job.aiState[$0] == .running }
                               : job.pages.filter { job.pageState[$0] == .running }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Live output").font(.headline)
                Spacer()
                Text("\(donePages.count) of \(job.pages.count) pages").font(.caption).foregroundStyle(.secondary).monospacedDigit()
                Toggle(isOn: $follow) { Text("Follow") }.toggleStyle(.switch).controlSize(.mini)
            }
            .padding(.horizontal, 16).padding(.vertical, 10)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if donePages.isEmpty { Waiting() }
                        ForEach(donePages, id: \.self) { n in
                            if let r = job.results[n] {
                                PageCard(number: n, result: r, ai: job.aiState[n], kept: job.aiKept[n] ?? 0, outDir: job.outDir)
                                    .id(n)
                                    .transition(.asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity),
                                                            removal: .opacity))
                            }
                        }
                        if !running.isEmpty { ReadingRow(pages: running, ai: job.phase == .refining).id("reading") }
                    }
                    .padding(16)
                }
                .onChange(of: donePages.count) { _ in
                    guard follow else { return }
                    withAnimation(.easeInOut(duration: 0.5)) { proxy.scrollTo("reading", anchor: .bottom) }
                }
            }
        }
    }
}

struct Waiting: View {
    @State private var on = false
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: "text.viewfinder").font(.system(size: 34, weight: .light))
            Text("The first page will appear here as soon as it's read.").multilineTextAlignment(.center)
        }
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity).padding(.vertical, 60)
        .opacity(on ? 1 : 0.55)
        .onAppear { withAnimation(.easeInOut(duration: 1.4).repeatForever(autoreverses: true)) { on = true } }
    }
}

struct ReadingRow: View {
    let pages: [Int]
    var ai = false

    private var label: String {
        let verb = ai ? "Refining" : "Reading"
        let noun = pages.count == 1 ? "page" : "pages"
        let list = pages.prefix(6).map(String.init).joined(separator: ", ")
        return "\(verb) \(noun) \(list)" + (pages.count > 6 ? "…" : "")
    }
    var body: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(label).foregroundStyle(.secondary)
        }
        .font(.callout)
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(.quaternary.opacity(0.5), in: Capsule())
    }
}

struct PageCard: View {
    let number: Int
    let result: PageResult
    var ai: AIState?
    var kept = 0
    let outDir: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("Page \(number)").font(.subheadline.weight(.semibold))
                Chip(icon: result.kind == "ocr" ? "eye" : "textformat",
                     text: result.kind == "ocr" ? "OCR" : "Text layer",
                     tint: result.kind == "ocr" ? .purple : .secondary)
                if let c = result.confidence {
                    Chip(icon: "checkmark.seal", text: "\(Int(c))%", tint: c >= 80 ? .green : (c >= 60 ? .orange : .red))
                }
                switch ai {
                case .corrected(let share)?:
                    Chip(icon: "sparkles", text: "AI edited \(max(1, Int((share * 100).rounded())))%"
                         + (kept > 0 ? " · \(kept) passage\(kept == 1 ? "" : "s") kept" : ""), tint: kept > 0 ? .orange : .purple)
                case .unchanged?: Chip(icon: "sparkles", text: "AI: no changes needed", tint: .secondary)
                case .failed(let why)?: Chip(icon: "exclamationmark.triangle", text: "AI kept original · \(why)", tint: .orange)
                default: EmptyView()
                }
                Spacer()
            }
            if result.markdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Text("No text on this page").font(.callout).foregroundStyle(.tertiary)
            } else {
                MarkdownLite(markdown: result.markdown, outDir: outDir)
                    .id(result.markdown)
                    .transition(.opacity)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.background, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.primary.opacity(0.08)))
        .shadow(color: .black.opacity(0.05), radius: 6, y: 2)
    }
}

/// Small, fast Markdown renderer for the live feed (headings, lists, tables, figures, inline styles).
struct MarkdownLite: View {
    let markdown: String
    let outDir: String?

    private var blocks: [String] {
        markdown.components(separatedBy: "\n\n").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, b in block(b) }
        }
    }

    @ViewBuilder private func block(_ b: String) -> some View {
        if b.hasPrefix("!["), let path = b.range(of: "](").map({ String(b[$0.upperBound...]).prefix(while: { $0 != ")" }) }) {
            if let outDir {
                Thumbnail(url: URL(fileURLWithPath: outDir).appendingPathComponent(String(path)))
                    .frame(maxWidth: .infinity, minHeight: 90, maxHeight: 220)
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
            }
        } else if b.hasPrefix("#") {
            let level = b.prefix(while: { $0 == "#" }).count
            Text(inline(String(b.drop(while: { $0 == "#" || $0 == " " }))))
                .font(level == 1 ? .title2.weight(.bold) : level == 2 ? .title3.weight(.semibold) : .headline)
        } else if b.hasPrefix("|") {
            Text(b).font(.system(size: 11, design: .monospaced)).textSelection(.enabled)
                .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
        } else if b.hasPrefix("*") && b.hasSuffix("*") && !b.hasPrefix("**") {
            Text(inline(b)).font(.callout).foregroundStyle(.secondary)
        } else {
            Text(inline(b)).font(.body).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func inline(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(s)
    }
}

// MARK: Page thumbnails (PDFKit, one serial queue per document)

final class Thumbnailer: ObservableObject, @unchecked Sendable {
    private let doc: PDFDocument?
    private let imageURL: URL?
    private let queue = DispatchQueue(label: "ocrstudio.thumbs", qos: .utility)
    private let cache = NSCache<NSNumber, NSImage>()

    init(url: URL) {
        if url.pathExtension.lowercased() == "pdf" { doc = PDFDocument(url: url); imageURL = nil }
        else { doc = nil; imageURL = url }
    }

    func image(for page: Int) async -> NSImage? {
        if let hit = cache.object(forKey: NSNumber(value: page)) { return hit }
        return await withCheckedContinuation { cont in
            queue.async { [self] in
                var img: NSImage?
                if let p = doc?.page(at: page - 1) {
                    img = p.thumbnail(of: CGSize(width: 200, height: 280), for: .mediaBox)
                } else if let imageURL {
                    img = NSImage(contentsOf: imageURL)
                }
                if let img { cache.setObject(img, forKey: NSNumber(value: page)) }
                cont.resume(returning: img)
            }
        }
    }
}
