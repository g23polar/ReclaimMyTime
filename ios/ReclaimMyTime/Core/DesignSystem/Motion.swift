import SwiftUI

/// Motion tokens (design/tokens.md "Motion"). Motion is decorative only; state is always shown
/// statically as well.
enum Motion {
    /// Press states, toggles.
    static let quick = Animation.easeOut(duration: 0.15)
    /// Banners in/out, sheet content changes.
    static let standard = Animation.snappy(duration: 0.25)
    /// Moved blocks gliding old → new in diff review.
    static let move = Animation.smooth(duration: 0.45)
    /// Reduce Motion substitute for `move`: a 0.2 s crossfade; nothing slides.
    static let moveReduced = Animation.easeInOut(duration: 0.2)

    /// `move`, or the crossfade timing when Reduce Motion is on. Pair the reduced form with an
    /// `.opacity` transition instead of a positional one.
    static func move(reduceMotion: Bool) -> Animation {
        reduceMotion ? moveReduced : move
    }
    // `motion.stream` (chat streaming) is "append only, no easing": apply no animation.
}
