import Foundation

/// Talks to Ollama / OpenRouter for the Settings screen (model lists, connection test).
enum AIService {
    static let openRouterSuggestions = [
        "google/gemini-2.0-flash-001",
        "google/gemini-2.5-flash",
        "anthropic/claude-haiku-4.5",
        "openai/gpt-4o-mini",
        "meta-llama/llama-3.3-70b-instruct",
        "qwen/qwen-2.5-72b-instruct",
    ]

    static func ollamaModels(baseURL: String) async -> Result<[String], Error> {
        guard let url = URL(string: baseURL.trimmingCharacters(in: .whitespaces).trimmingSuffix("/") + "/api/tags") else {
            return .failure(AIServiceError("Invalid address"))
        }
        var req = URLRequest(url: url); req.timeoutInterval = 4
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw AIServiceError("Ollama didn't answer") }
            struct Tags: Decodable { struct M: Decodable { let name: String }; let models: [M] }
            let names = try JSONDecoder().decode(Tags.self, from: data).models.map(\.name).sorted()
            return .success(names)
        } catch {
            return .failure(AIServiceError("Can't reach Ollama at \(baseURL). Is it running? (open the Ollama app or run `ollama serve`)"))
        }
    }

    static func testOpenRouter(key: String) async -> Result<String, Error> {
        guard !key.isEmpty, let url = URL(string: "https://openrouter.ai/api/v1/auth/key") else {
            return .failure(AIServiceError("Enter an API key first"))
        }
        var req = URLRequest(url: url); req.timeoutInterval = 8
        req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            switch (resp as? HTTPURLResponse)?.statusCode {
            case 200: return .success("Key accepted")
            case 401, 403: return .failure(AIServiceError("OpenRouter rejected this key"))
            default: return .failure(AIServiceError("OpenRouter returned an unexpected answer"))
            }
        } catch {
            return .failure(AIServiceError("Couldn't reach OpenRouter — check your connection"))
        }
    }
}

struct AIServiceError: LocalizedError {
    let message: String
    init(_ m: String) { message = m }
    var errorDescription: String? { message }
}

extension String {
    func trimmingSuffix(_ s: String) -> String { hasSuffix(s) ? String(dropLast(s.count)) : self }
}
