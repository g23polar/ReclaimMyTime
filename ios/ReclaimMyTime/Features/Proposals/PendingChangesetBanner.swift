import SwiftUI

/// The pending-changeset banner (timetable.md §6): `surfaceRaised`, 1 pt `proposal` border,
/// `callout` text, `sparkles`. Not dismissible: the user confirms or keeps their week.
struct PendingChangesetBanner: View {
    let changeset: Changeset
    let onReview: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        let text = ProposalSummary.make(changeset)
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.s))
            : AnyLayout(HStackLayout(spacing: Spacing.s))
        layout {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                Image(systemName: "sparkles")
                    .foregroundStyle(ColorToken.proposal)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    Text(text.summary)
                        .foregroundStyle(ColorToken.textPrimary)
                    if text.doesNotFit {
                        Label("doesn't all fit", systemImage: "exclamationmark.triangle")
                            .foregroundStyle(ColorToken.warning)
                    }
                }
                .font(.rmt.callout)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Proposal: \(text.summary)\(text.doesNotFit ? ", doesn't all fit" : "")")
            if !dynamicTypeSize.isAccessibilitySize { Spacer(minLength: Spacing.s) }
            Button("Review", action: onReview)
                .buttonStyle(.borderedProminent)
                .tint(ColorToken.proposal)
                .frame(minHeight: HitTarget.min)
                .accessibilityHint("Opens the proposed changes")
        }
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.s)
        .background(ColorToken.surfaceRaised, in: RoundedRectangle(cornerRadius: Radius.card))
        .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.proposal, lineWidth: 1))
        .padding(.horizontal, Spacing.l)
        .padding(.bottom, Spacing.s)
    }
}

private struct PendingChangesetBannerModifier<Accessory: View>: ViewModifier {
    /// Bottom chrome that must sit above the banner in the same inset (the chat composer). Two
    /// separate bottom insets don't compose reliably, so they share one.
    let accessory: Accessory
    @Environment(AppModel.self) private var model
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .safeAreaInset(edge: .bottom, spacing: 0) {
                VStack(spacing: 0) {
                    accessory
                    if let changeset = model.pending.pending {
                        PendingChangesetBanner(changeset: changeset) { model.navigation.presentReview() }
                            .padding(.top, Spacing.s)
                            .transition(reduceMotion ? .opacity : .move(edge: .bottom).combined(with: .opacity))
                    }
                }
                .background(.bar)
            }
            .animation(Motion.standard, value: model.pending.pending?.id)
    }
}

extension View {
    /// Pins the pending-changeset banner above the tab bar.
    func pendingChangesetBanner() -> some View {
        modifier(PendingChangesetBannerModifier(accessory: EmptyView()))
    }

    /// Banner with bottom chrome (e.g. a composer) stacked above it in the same inset.
    func pendingChangesetBanner<Accessory: View>(@ViewBuilder above accessory: () -> Accessory) -> some View {
        modifier(PendingChangesetBannerModifier(accessory: accessory()))
    }
}

/// Transient notices from the store (e.g. 409 out of date), shown as a toast at the top.
private struct ChangesetNoticeToastModifier: ViewModifier {
    @Environment(AppModel.self) private var model

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .top) {
                if let notice = model.pending.notice {
                    Text(notice.message)
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.textPrimary)
                        .padding(Spacing.m)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(ColorToken.surfaceRaised, in: RoundedRectangle(cornerRadius: Radius.card))
                        .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.gridLine, lineWidth: 1))
                        .padding(.horizontal, Spacing.l)
                        .padding(.top, Spacing.s)
                        .onTapGesture { model.pending.dismissNotice() }
                        .accessibilityAddTraits(.isButton)
                        .accessibilityHint("Dismisses this message")
                        .transition(.opacity)
                        .task(id: notice) {
                            AccessibilityNotification.Announcement(notice.message).post()
                            try? await Task.sleep(for: .seconds(5))
                            model.pending.dismissNotice()
                        }
                }
            }
            .animation(Motion.standard, value: model.pending.notice)
    }
}

extension View {
    func changesetNoticeToast() -> some View {
        modifier(ChangesetNoticeToastModifier())
    }
}
