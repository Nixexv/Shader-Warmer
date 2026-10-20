import Foundation

/// A stable key and its values are kept separate, so paths and game names are never translated.
struct LocalizedText: ExpressibleByStringLiteral, ExpressibleByStringInterpolation {
    var key: String
    var values: [String]

    init(stringLiteral value: String) { key = value; values = [] }
    init(stringInterpolation: StringInterpolation) {
        key = stringInterpolation.key; values = stringInterpolation.values
    }

    struct StringInterpolation: StringInterpolationProtocol {
        var key = ""
        var values: [String] = []
        init(literalCapacity: Int, interpolationCount: Int) {
            key.reserveCapacity(literalCapacity); values.reserveCapacity(interpolationCount)
        }
        mutating func appendLiteral(_ literal: String) { key += literal }
        mutating func appendInterpolation<T>(_ value: T) {
            key += "{\(values.count)}"; values.append(String(describing: value))
        }
    }
}

struct AppLocalizer {
    let language: String
    let english: [String: String]
    private let messages: [(regex: NSRegularExpression, translation: String, indices: [Int])]
    private static let token = try! NSRegularExpression(pattern: #"\{(\d+)\}"#)

    /// Walk the preferred language list, including regional/script variants. Default to English.
    static func language(for preferences: [String]) -> String {
        for preference in preferences {
            let base = preference.lowercased().split(whereSeparator: { $0 == "-" || $0 == "_" }).first
            if base == "ru" { return "ru" }
            if base == "en" { return "en" }
        }
        return "en"
    }

    init(preferences: [String], catalogURL: URL?) {
        language = Self.language(for: preferences)
        english = catalogURL.flatMap { try? Data(contentsOf: $0) }
            .flatMap { try? JSONDecoder().decode([String: String].self, from: $0) } ?? [:]
        // Engine messages arrive as already formatted strings. Match only entire known messages,
        // most specific first; leave the source logs and all unknown diagnostic text intact.
        messages = english.sorted {
            let left = Self.token.stringByReplacingMatches(in: $0.key, range: NSRange($0.key.startIndex..., in: $0.key), withTemplate: "").count
            let right = Self.token.stringByReplacingMatches(in: $1.key, range: NSRange($1.key.startIndex..., in: $1.key), withTemplate: "").count
            return left == right ? $0.key < $1.key : left > right
        }.compactMap { entry in
            let tokens = Self.token.matches(in: entry.key, range: NSRange(entry.key.startIndex..., in: entry.key))
            guard !tokens.isEmpty else { return nil }
            var pattern = #"\A"#; var cursor = entry.key.startIndex; var indices: [Int] = []
            for token in tokens {
                guard let range = Range(token.range, in: entry.key), let number = Range(token.range(at: 1), in: entry.key),
                      let index = Int(entry.key[number]) else { return nil }
                pattern += NSRegularExpression.escapedPattern(for: String(entry.key[cursor..<range.lowerBound])) + "(.*?)"
                cursor = range.upperBound; indices.append(index)
            }
            pattern += NSRegularExpression.escapedPattern(for: String(entry.key[cursor...])) + #"\z"#
            guard let regex = try? NSRegularExpression(pattern: pattern, options: [.dotMatchesLineSeparators]) else { return nil }
            return (regex, entry.value, indices)
        }
    }

    func text(_ text: LocalizedText) -> String {
        Self.render(language == "ru" ? text.key : english[text.key] ?? text.key, values: text.values)
    }

    func message(_ source: String) -> String {
        guard language == "en", !source.isEmpty else { return source }
        if let translated = english[source] { return translated }
        for rule in messages {
            guard let match = rule.regex.firstMatch(in: source, range: NSRange(source.startIndex..., in: source)) else { continue }
            var values = Array(repeating: "", count: (rule.indices.max() ?? -1) + 1)
            for (capture, index) in rule.indices.enumerated() {
                guard let range = Range(match.range(at: capture + 1), in: source) else { continue }
                values[index] = String(source[range])
            }
            return Self.render(rule.translation, values: values)
        }
        return source
    }

    private static func render(_ template: String, values: [String]) -> String {
        var output = ""; var cursor = template.startIndex
        for match in token.matches(in: template, range: NSRange(template.startIndex..., in: template)) {
            guard let range = Range(match.range, in: template), let number = Range(match.range(at: 1), in: template),
                  let index = Int(template[number]), values.indices.contains(index) else { continue }
            output += template[cursor..<range.lowerBound]; output += values[index]; cursor = range.upperBound
        }
        output += template[cursor...]
        return output
    }
}

enum L10n {
    static let current = AppLocalizer(preferences: Locale.preferredLanguages,
                                     catalogURL: Bundle.main.url(forResource: "Localizations", withExtension: "json"))
    static func message(_ source: String) -> String { current.message(source) }
}

func L(_ text: LocalizedText) -> String { L10n.current.text(text) }
