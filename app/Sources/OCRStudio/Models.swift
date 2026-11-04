import Foundation

enum JobStatus: Equatable {
    case queued, running, done, failed(String), cancelled

    var label: String {
        switch self {
        case .queued: return "Queued"
        case .running: return "Converting"
        case .done: return "Done"
        case .failed: return "Failed"
        case .cancelled: return "Cancelled"
        }
    }
}

struct Report: Decodable, Equatable {
    var markdown: String
    var preview: String?
    var outDir: String
    var method: String
    var pages: Int
    var textPages: Int
    var ocrPages: Int
    var figures: Int
    var tables: Int
    var removedRunning: Int
    var avgConfidence: Double?
    var lowConfidencePages: [Int]
    var words: Int
    var seconds: Double

    enum CodingKeys: String, CodingKey {
        case markdown, preview, method, pages, figures, tables, words, seconds
        case outDir = "out_dir"
        case textPages = "text_pages"
        case ocrPages = "ocr_pages"
        case removedRunning = "removed_running"
        case avgConfidence = "avg_confidence"
        case lowConfidencePages = "low_confidence_pages"
    }
}

struct Job: Identifiable, Equatable {
    let id = UUID()
    let url: URL
    var status: JobStatus = .queued
    var done = 0
    var total = 0
    var ocrPages = 0
    var lang = ""
    var report: Report?
    var log: [String] = []

    var name: String { url.lastPathComponent }
    var fraction: Double { total > 0 ? Double(done) / Double(total) : 0 }
    var isFinished: Bool {
        switch status { case .done, .failed, .cancelled: return true; default: return false }
    }
}

enum OCRMode: String, CaseIterable, Identifiable {
    case auto, force, off
    var id: String { rawValue }
    var title: String {
        switch self {
        case .auto: return "Scanned pages only"
        case .force: return "Every page"
        case .off: return "Never"
        }
    }
}

/// What `python -m ocrmd --doctor` reports.
struct Health: Decodable, Equatable {
    var version: String?
    var tesseract: String?
    var languages: [String]
    var markitdown: Bool
}

/// One JSON line from the engine (`--json`).
struct EngineEvent: Decodable {
    var event: String
    var page: Int?
    var kind: String?
    var done: Int?
    var total: Int?
    var message: String?
    var code: String?
    var lang: String?
    var ocrPageCount: Int?

    enum CodingKeys: String, CodingKey {
        case event, page, kind, done, total, message, code, lang
        case ocrPages = "ocr_pages"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        event = try c.decode(String.self, forKey: .event)
        page = try? c.decode(Int.self, forKey: .page)
        kind = try? c.decode(String.self, forKey: .kind)
        done = try? c.decode(Int.self, forKey: .done)
        total = try? c.decode(Int.self, forKey: .total)
        message = try? c.decode(String.self, forKey: .message)
        code = try? c.decode(String.self, forKey: .code)
        lang = try? c.decode(String.self, forKey: .lang)
        // "start" sends the list of scanned pages, "done" sends a count.
        if let list = try? c.decode([Int].self, forKey: .ocrPages) { ocrPageCount = list.count }
        else { ocrPageCount = try? c.decode(Int.self, forKey: .ocrPages) }
    }
}

let supportedExtensions: Set<String> = [
    "pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif",
    "docx", "pptx", "xlsx", "xls", "html", "htm", "epub", "csv", "txt", "md",
]
