import SwiftUI

/// Seam for the intent-narrative panels (PRD §4.1, design/onboarding.md §1).
///
/// TODO(product owner): the product owner builds the real panels, doodles and animation. To swap
/// them in, make the new view conform to `OnboardingIntro` and point `OnboardingIntroView` at it.
/// Contract: call `onFinish` from both **Continue** (last panel) and **Skip** (visible on every
/// panel); `RootView` then shows sign-in. Nothing else depends on the intro.
protocol OnboardingIntro: View {
    init(onFinish: @escaping @MainActor () -> Void)
}

typealias OnboardingIntroView = IntentNarrativePlaceholderView

/// Placeholder only: no panels, doodles or animation (owned by the product owner).
struct IntentNarrativePlaceholderView: OnboardingIntro {
    private let onFinish: @MainActor () -> Void

    init(onFinish: @escaping @MainActor () -> Void) {
        self.onFinish = onFinish
    }

    var body: some View {
        VStack(spacing: Spacing.xxl) {
            HStack {
                Spacer()
                Button("Skip", action: onFinish)
                    .frame(minWidth: HitTarget.min, minHeight: HitTarget.min)
            }
            Spacer()
            Text("Your week, reclaimed.")
                .font(.rmt.display)
                .foregroundStyle(ColorToken.textPrimary)
                .multilineTextAlignment(.center)
            Text("Intro panels coming soon.")
                .font(.rmt.callout)
                .foregroundStyle(ColorToken.textSecondary)
            Spacer()
            Button(action: onFinish) {
                Text("Continue").frame(maxWidth: .infinity, minHeight: HitTarget.min)
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(Spacing.l)
    }
}

#Preview {
    OnboardingIntroView(onFinish: {})
}
