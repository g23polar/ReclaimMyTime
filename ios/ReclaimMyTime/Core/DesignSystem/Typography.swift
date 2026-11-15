import SwiftUI

/// Type tokens (design/tokens.md "Type"). All are system text styles, so Dynamic Type scales them
/// through AX5. Namespaced under `.rmt` because several token names (`title`, `body`, …) collide
/// with SwiftUI's own `Font` statics. Usage: `.font(.rmt.blockTitle)`.
struct FontTokens: Sendable {
    /// Onboarding headlines, empty-state titles.
    let display = Font.system(.largeTitle, design: .rounded, weight: .bold)
    /// Screen titles inside sheets.
    let title = Font.system(.title2, weight: .semibold)
    /// Card titles, trade-off sheet rows.
    let headline = Font.system(.headline, weight: .semibold)
    /// Default text.
    let body = Font.system(.body)
    /// Chat bubbles (both sides).
    let chat = Font.system(.body, design: .rounded)
    /// Banner text, helper copy.
    let callout = Font.system(.callout)
    /// Project name inside a block.
    let blockTitle = Font.system(.footnote, weight: .semibold)
    /// Duration and times inside a block.
    let blockMeta = Font.system(.caption2).monospacedDigit()
    /// Hour labels, weekday headers.
    let gridLabel = Font.system(.caption2, weight: .medium).monospacedDigit()
    /// Hours in the trade-off sheet.
    let numeral = Font.system(.title3, weight: .semibold).monospacedDigit()
}

extension Font {
    static let rmt = FontTokens()
}
