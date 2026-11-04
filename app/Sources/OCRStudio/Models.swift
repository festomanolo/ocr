import Foundation
import PDFKit

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

enum PageState: Equatable { case pending, running, done }

/// Outcome of the optional AI correction for one page.
enum AIState: Equatable {
    case running
    case corrected(Double)      // share of words changed
    case unchanged
    case skipped(String)
    case failed(String)
}

enum Phase: Equatable { case reading, refining }

struct AIReport: Decodable, Equatable {
    var provider: String
    var model: String
    var corrected: Int
    var unchanged: Int
    var skipped: Int
    var failed: Int
    var keptPassages: Int?
    var firstError: String
    var seconds: Double
    enum CodingKeys: String, CodingKey {
        case provider, model, corrected, unchanged, skipped, failed, seconds
        case keptPassages = "kept_passages"
        case firstError = "first_error"
    }
}

struct PageResult: Equatable {
    var vision = false          // re-read by the vision model
    var markdown: String
    var kind: String            // "text" | "ocr"
    var confidence: Double?
    var figures: [String]
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
    var ai: AIReport?
    var visionPages: [Int]?
    var visionModel: String?

    enum CodingKeys: String, CodingKey {
        case markdown, preview, method, pages, figures, tables, words, seconds, ai
        case visionPages = "vision_pages"
        case visionModel = "vision_model"
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
    /// "1-20, 45" — empty means every page.
    var pageRange = ""
    // Live progress
    var pages: [Int] = []
    var pageState: [Int: PageState] = [:]
    var results: [Int: PageResult] = [:]
    var workers = 0
    var startedAt: Date?
    var outDir: String?
    // AI correction
    var useAI = false
    var phase: Phase = .reading
    var aiTotal = 0
    var aiDone = 0
    var aiModel = ""
    var aiProvider = ""
    var aiWorkers = 0
    var aiStartedAt: Date?
    var aiState: [Int: AIState] = [:]
    var aiKept: [Int: Int] = [:]
    var pageCount: Int?

    init(url: URL) {
        self.url = url
        self.pageCount = url.pathExtension.lowercased() == "pdf" ? PDFDocument(url: url)?.pageCount : nil
    }

    var name: String { url.lastPathComponent }
    var isPDF: Bool { url.pathExtension.lowercased() == "pdf" }
    var rangeIsValid: Bool { PageRange.parse(pageRange, max: pageCount) != nil }
    var fraction: Double {
        if phase == .refining { return aiTotal > 0 ? Double(aiDone) / Double(aiTotal) : 1 }
        return total > 0 ? Double(done) / Double(total) : 0
    }
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
    var pages: [Int]?
    var md: String?
    var figures: [String]?
    var conf: Double?
    var workers: Int?
    var outDir: String?
    var status: String?
    var reason: String?
    var changed: Double?
    var kept: Int?
    var model: String?
    var provider: String?

    enum CodingKeys: String, CodingKey {
        case event, page, kind, done, total, message, code, lang, pages, md, figures, conf, workers
        case status, reason, changed, kept, model, provider
        case outDir = "out_dir"
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
        pages = try? c.decode([Int].self, forKey: .pages)
        md = try? c.decode(String.self, forKey: .md)
        figures = try? c.decode([String].self, forKey: .figures)
        conf = try? c.decode(Double.self, forKey: .conf)
        workers = try? c.decode(Int.self, forKey: .workers)
        outDir = try? c.decode(String.self, forKey: .outDir)
        status = try? c.decode(String.self, forKey: .status)
        reason = try? c.decode(String.self, forKey: .reason)
        changed = try? c.decode(Double.self, forKey: .changed)
        kept = try? c.decode(Int.self, forKey: .kept)
        model = try? c.decode(String.self, forKey: .model)
        provider = try? c.decode(String.self, forKey: .provider)
        // "start" sends the list of scanned pages, "done" sends a count.
        if let list = try? c.decode([Int].self, forKey: .ocrPages) { ocrPageCount = list.count }
        else { ocrPageCount = try? c.decode(Int.self, forKey: .ocrPages) }
    }
}

let supportedExtensions: Set<String> = [
    "pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif",
    "docx", "pptx", "xlsx", "xls", "html", "htm", "epub", "csv", "txt", "md",
]

enum PageRange {
    /// Parses "1-5, 9, 12-" into a normalised string, or nil if invalid. Empty = all pages (returns "").
    static func parse(_ text: String, max: Int?) -> String? {
        let t = text.trimmingCharacters(in: .whitespaces)
        if t.isEmpty { return "" }
        var parts: [String] = []
        for raw in t.split(separator: ",") {
            let p = raw.trimmingCharacters(in: .whitespaces)
            let bits = p.split(separator: "-", omittingEmptySubsequences: false).map { $0.trimmingCharacters(in: .whitespaces) }
            switch bits.count {
            case 1:
                guard let a = Int(bits[0]), a >= 1, max == nil || a <= max! else { return nil }
                parts.append("\(a)")
            case 2:
                guard let a = Int(bits[0]), a >= 1 else { return nil }
                let b = bits[1].isEmpty ? max : Int(bits[1])          // "12-" = to the end
                guard let b, b >= a, max == nil || a <= max! else { return nil }
                parts.append("\(a)-\(Swift.min(b, max ?? b))")
            default: return nil
            }
        }
        return parts.joined(separator: ",")
    }
}
