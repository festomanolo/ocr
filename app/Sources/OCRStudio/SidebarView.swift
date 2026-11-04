import SwiftUI

struct SidebarView: View {
    @EnvironmentObject var store: JobStore

    var body: some View {
        VStack(spacing: 0) {
            if store.jobs.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "tray").font(.largeTitle).foregroundStyle(.tertiary)
                    Text("No files yet").foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(selection: $store.selection) {
                    ForEach(store.jobs) { job in
                        JobRow(job: job)
                            .tag(job.id)
                            .contextMenu {
                                if job.report != nil {
                                    Button("Show in Finder") { store.reveal(job) }
                                    Button("Copy Markdown") { store.copyMarkdown(job) }
                                }
                                if job.isFinished, job.report == nil { Button("Retry") { store.retry(job.id) } }
                                Divider()
                                Button("Remove", role: .destructive) { store.remove(job.id) }
                            }
                    }
                }
                .listStyle(.sidebar)
            }
            Divider()
            HStack {
                Text(summary).font(.caption).foregroundStyle(.secondary)
                Spacer()
                if store.jobs.contains(where: \.isFinished) {
                    Button("Clear") { store.clearFinished() }.buttonStyle(.borderless).font(.caption)
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
        }
    }

    private var summary: String {
        let done = store.jobs.filter { $0.status == .done }.count
        return store.jobs.isEmpty ? "Drop files anywhere" : "\(done) of \(store.jobs.count) converted"
    }
}

struct JobRow: View {
    let job: Job

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.title3)
                .foregroundStyle(tint)
                .frame(width: 26)
            VStack(alignment: .leading, spacing: 3) {
                Text(job.name).lineLimit(1).truncationMode(.middle)
                switch job.status {
                case .running:
                    ProgressView(value: job.fraction).controlSize(.small).tint(job.phase == .refining ? .purple : .accentColor)
                    Text(job.phase == .refining ? "AI refining \(job.aiDone) of \(job.aiTotal)"
                         : (job.total > 0 ? "Page \(job.done) of \(job.total)" : "Starting…"))
                        .font(.caption).foregroundStyle(job.phase == .refining ? Color.purple : .secondary)
                case .done:
                    if let r = job.report {
                        Text(subtitle(r)).font(.caption).foregroundStyle(.secondary)
                    }
                case .failed(let msg):
                    Text(msg).font(.caption).foregroundStyle(.red).lineLimit(1)
                default:
                    Text(job.status.label).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 3)
    }

    private func subtitle(_ r: Report) -> String {
        var parts = ["\(r.pages) page\(r.pages == 1 ? "" : "s")"]
        if r.ocrPages > 0 { parts.append("\(r.ocrPages) OCR") }
        if r.figures > 0 { parts.append("\(r.figures) fig") }
        return parts.joined(separator: " · ")
    }

    private var icon: String {
        switch job.status {
        case .done: return "checkmark.circle.fill"
        case .failed: return "exclamationmark.triangle.fill"
        case .running: return "doc.text.magnifyingglass"
        case .cancelled: return "xmark.circle"
        case .queued: return "clock"
        }
    }

    private var tint: Color {
        switch job.status {
        case .done: return .green
        case .failed: return .red
        case .running: return .accentColor
        default: return .secondary
        }
    }
}
