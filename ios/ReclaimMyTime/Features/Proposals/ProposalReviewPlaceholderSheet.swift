import SwiftUI

/// Placeholder for the proposal review screen (T5.3, diff-and-infeasibility.md §1–§4). It shows the
/// banner summary and, with no plan yet (onboarding), the staged project cards; Confirm and
/// "Keep my week" are real.
// TODO(T5.3): replace with the diff grid, infeasibility strip and trade-off sheet.
struct ProposalReviewPlaceholderSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var openedId: UUID?
    @State private var confirmedCount = 0

    var body: some View {
        NavigationStack {
            Group {
                if let changeset = model.pending.pending {
                    content(changeset)
                } else {
                    ProgressView()
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
        }
        .presentationDetents([.large])
        .sensoryFeedback(.success, trigger: confirmedCount)
        .onAppear { openedId = model.pending.pending?.id }
        .onChange(of: model.pending.pending?.id) { _, newId in
            // Confirmed, rejected or 409'd elsewhere: nothing left to review.
            if newId == nil { dismiss() }
        }
    }

    private var title: String {
        guard let changeset = model.pending.pending else { return "Review" }
        if changeset.planId != nil, changeset.diff?.againstPlanId == nil { return "Your first week" }
        return "Review changes"
    }

    @ViewBuilder
    private func content(_ changeset: Changeset) -> some View {
        let summary = ProposalSummary.make(changeset)
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.l) {
                if let openedId, openedId != changeset.id {
                    Text("Updated with your latest change.")
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.proposal)
                }
                Text(changeset.narration ?? summary.summary)
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.textSecondary)
                if summary.doesNotFit {
                    Label("Not everything fits this week. The full trade-off view is coming.", systemImage: "exclamationmark.triangle")
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.warning)
                }
                if changeset.planId == nil {
                    let cards = ProjectCardDerivation.cards(from: changeset, base: { model.projects.card(for: $0) })
                    ForEach(cards) { card in
                        ProjectSummaryCardView(card: card, slot: model.palette.slot(for: card.id), onEdit: nil)
                    }
                } else {
                    Text("The week view of this proposal arrives in a later update.")
                        .font(.rmt.body)
                        .foregroundStyle(ColorToken.textSecondary)
                }
                if let error = model.pending.actionError {
                    Text(error)
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.warning)
                }
            }
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .safeAreaInset(edge: .bottom) {
            ConfirmRejectBar(
                confirmTitle: summary.doesNotFit ? "Confirm what fits" : "Confirm changes",
                isBusy: model.pending.isResolving,
                onReject: { Task { if await model.pending.reject(id: changeset.id) { dismiss() } } },
                onConfirm: {
                    Task {
                        if await model.pending.confirm(id: changeset.id) {
                            confirmedCount += 1
                            dismiss()
                        }
                    }
                }
            )
        }
    }
}

/// diff-and-infeasibility.md §3: "Keep my week" (bordered) + confirm (`borderedProminent`).
struct ConfirmRejectBar: View {
    let confirmTitle: String
    let isBusy: Bool
    let onReject: () -> Void
    let onConfirm: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        let layout = dynamicTypeSize.isAccessibilitySize ? AnyLayout(VStackLayout(spacing: Spacing.s)) : AnyLayout(HStackLayout(spacing: Spacing.s))
        layout {
            Button(action: onReject) {
                Text("Keep my week").frame(maxWidth: .infinity, minHeight: HitTarget.min)
            }
            .buttonStyle(.bordered)
            Button(action: onConfirm) {
                Text(confirmTitle).frame(maxWidth: .infinity, minHeight: HitTarget.min)
            }
            .buttonStyle(.borderedProminent)
        }
        .disabled(isBusy)
        .padding(Spacing.l)
        .background(.bar)
    }
}
