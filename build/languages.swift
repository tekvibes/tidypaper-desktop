// Every ISO 639-1 language Apple knows: English name, its own name, script (maximal subtag) and a few of its letters.
// Run by build/make_languages.py, which writes prompts.json translate.other_languages.  Usage: swift languages.swift OUT.json
import Foundation
let en = Locale(identifier: "en")
var out: [[String: String]] = []
// ISO 639-1, plus widely used languages that only have a three-letter code
let extra = ["fil", "yue", "ceb", "haw", "ckb"]
for code in Locale.LanguageCode.isoLanguageCodes.map(\.identifier).filter({ $0.count == 2 }) + extra {
    guard let name = en.localizedString(forLanguageCode: code) else { continue }
    let own = Locale(identifier: code).localizedString(forLanguageCode: code) ?? ""
    let max = Locale.Language(identifier: code).maximalIdentifier
    let script = max.split(separator: "-").count > 1 ? String(max.split(separator: "-")[1]) : ""
    var sample = ""
    if let set = (Locale(identifier: code) as NSLocale).object(forKey: .exemplarCharacterSet) as? CharacterSet {
        var n = 0
        for v in 0x41...0x1FFFF where n < 12 {
            if let u = Unicode.Scalar(v), set.contains(u), u.properties.isAlphabetic { sample.unicodeScalars.append(u); n += 1 }
        }
    }
    out.append(["code": code, "name": name, "own": own, "script": script, "sample": sample])
}
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys, .prettyPrinted])
FileManager.default.createFile(atPath: CommandLine.arguments[1], contents: data)
print(out.count)
