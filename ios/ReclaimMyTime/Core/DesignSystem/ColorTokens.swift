import SwiftUI

/// Semantic colors live in Assets.xcassets/Colors (light + dark appearances), surfaced through
/// Xcode's generated `ColorResource` symbols: `Color(.bg)`, `.foregroundStyle(Color(.textSecondary))`.
/// Views reference token names only, never raw values.
enum ColorToken {
    static let bg = Color(.bg)
    static let surface = Color(.surface)
    static let surfaceRaised = Color(.surfaceRaised)
    static let textPrimary = Color(.textPrimary)
    static let textSecondary = Color(.textSecondary)
    static let gridLine = Color(.gridLine)
    static let gridLineHalf = Color(.gridLineHalf)
    static let nowLine = Color(.nowLine)
    static let busy = Color(.busy)
    static let accent = Color(.accent)
    static let onAccent = Color(.onAccent)
    static let proposal = Color(.proposal)
    static let warning = Color(.warning)
    static let positive = Color(.positive)
    static let destructive = Color(.destructive)
}
