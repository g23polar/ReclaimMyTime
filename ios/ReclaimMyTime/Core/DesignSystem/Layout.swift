import CoreGraphics

/// Spacing tokens (design/tokens.md "Spacing and radius").
enum Spacing {
    /// Inside blocks.
    static let xs: CGFloat = 4
    /// Between related items.
    static let s: CGFloat = 8
    /// Card padding (compact).
    static let m: CGFloat = 12
    /// Screen margins, card padding.
    static let l: CGFloat = 16
    /// Section gaps.
    static let xl: CGFloat = 24
    /// Onboarding vertical rhythm.
    static let xxl: CGFloat = 40
}

/// Corner radius tokens. Sheets use the system default radius (no token value).
enum Radius {
    /// Timetable blocks.
    static let block: CGFloat = 6
    /// Cards, banners, bubbles.
    static let card: CGFloat = 14
}

/// Minimum hit target for every tappable element (44 × 44 pt).
enum HitTarget {
    static let min: CGFloat = 44
}
