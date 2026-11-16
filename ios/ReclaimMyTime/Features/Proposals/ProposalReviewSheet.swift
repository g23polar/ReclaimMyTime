import SwiftUI

private struct DiffDayRoute: Hashable {
    let day: Date
}

/// Proposal review (diff-and-infeasibility.md §1–§4): narration, infeasibility strip, the week in
/// diff mode (or the list of changes), and the confirm/reject bar. Always shows the store's
/// *current* pending changeset, so a newer 202 swaps the content in place.
struct ProposalReviewSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityVoiceOverEnabled) private var voiceOverEnabled
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var openedId: UUID?
    @State private var confirmedCount = 0
    @State private var showsList: Bool?
    @State private var showsTradeOffs = false
    @State private var settled = false
    @State private var path: [DiffDayRoute] = []

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if let changeset = model.pending.pending {
                    content(changeset)
                } else {
                    ProgressView()
                }
            }
            .background(ColorToken.bg)
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
                if model.pending.pending?.plan != nil {
                    ToolbarItem(placement: .primaryAction) {
                        Button {
                            showsList = !isList
                        } label: {
                            Label(isList ? "Show week" : "Show list", systemImage: isList ? "calendar" : "list.bullet")
                        }
                    }
                }
            }
            .navigationDestination(for: DiffDayRoute.self) { route in
                if let changeset = model.pending.pending, let diff = DiffModel(changeset) {
                    let column = TimetableGeometry.columns(from: route.day, calendar: model.timetable.calendar, count: 1)
                    DiffGrid(diff: diff, columns: column, pointsPerHour: GridMetrics.dayPointsPerHour, settled: true)
                        .navigationTitle(model.timetable.timeText.dayTitle(route.day))
                        .navigationBarTitleDisplayMode(.inline)
                }
            }
        }
        .presentationDetents([.large])
        .sensoryFeedback(.success, trigger: confirmedCount)
        .onAppear {
            openedId = model.pending.pending?.id
            if model.navigation.tradeOffsRequested {
                model.navigation.tradeOffsRequested = false
                showsTradeOffs = model.pending.pending?.plan?.infeasibility != nil
            }
        }
        .onChange(of: model.pending.pending?.id) { _, newId in
            // Confirmed, rejected or 409'd elsewhere: nothing left to review.
            if newId == nil { dismiss() }
        }
        .task(id: model.pending.pending?.id) {
            // motion.move on open (and when a newer proposal swaps in).
            settled = false
            path = []
            try? await Task.sleep(for: .milliseconds(150))
            withAnimation(Motion.move(reduceMotion: reduceMotion)) { settled = true }
        }
    }

    private var isList: Bool { showsList ?? voiceOverEnabled }

    private var title: String {
        guard let changeset = model.pending.pending else { return "Review" }
        return DiffModel(changeset)?.title ?? "Review changes"
    }

    @ViewBuilder
    private func content(_ changeset: Changeset) -> some View {
        let summary = ProposalSummary.make(changeset)
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: Spacing.s) {
                if let openedId, openedId != changeset.id {
                    Label("Updated with your latest change.", systemImage: "sparkles")
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.proposal)
                }
                // drift / google_edit: say why the server proposed this (D25).
                if let headline = ChangesetReasonCopy.headline(changeset.reason) {
                    Label(headline, systemImage: "calendar.badge.exclamationmark")
                        .font(.rmt.callout.weight(.semibold))
                        .foregroundStyle(ColorToken.textPrimary)
                        .accessibilityAddTraits(.isHeader)
                }
                // Agent proposals have no narration (D8): their reply is already in chat.
                Text(changeset.narration ?? summary.summary)
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let infeasibility = changeset.plan?.infeasibility {
                    infeasibilityStrip(infeasibility, changeset: changeset)
                }
                if let error = model.pending.actionError {
                    Text(error)
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.warning)
                }
            }
            .padding(.horizontal, Spacing.l)
            .padding(.vertical, Spacing.s)
            .frame(maxWidth: .infinity, alignment: .leading)

            if let diff = DiffModel(changeset) {
                let columns = TimetableGeometry.columns(spanning: diff.plan.horizon, calendar: model.timetable.calendar)
                if isList {
                    DiffList(diff: diff, columns: columns)
                } else {
                    DiffGrid(diff: diff, columns: columns, pointsPerHour: GridMetrics.weekPointsPerHour, settled: settled) { column in
                        path.append(DiffDayRoute(day: column.start))
                    }
                }
            } else {
                // plan_id null (onboarding, no solve yet): the staged project cards instead.
                ScrollView {
                    VStack(spacing: Spacing.m) {
                        let cards = ProjectCardDerivation.cards(from: changeset, base: { model.projects.card(for: $0) })
                        ForEach(cards) { card in
                            ProjectSummaryCardView(card: card, slot: model.palette.slot(for: card.id), onEdit: nil)
                        }
                    }
                    .padding(Spacing.l)
                }
            }
        }
        .safeAreaInset(edge: .bottom) {
            ConfirmRejectBar(
                rejectTitle: ProposalReviewCopy.rejectTitle(changeset),
                rejectFootnote: ProposalReviewCopy.rejectFootnote(changeset),
                confirmTitle: ProposalReviewCopy.confirmTitle(changeset),
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
        .sheet(isPresented: $showsTradeOffs) {
            if let infeasibility = model.pending.pending?.plan?.infeasibility {
                TradeOffSheet(
                    infeasibility: infeasibility,
                    copy: TradeOffCopy(time: model.timetable.timeText, context: model.timetable.tradeOffContext(for: model.pending.pending)),
                    onTry: { await model.timetable.tryRelaxation($0) }
                )
            }
        }
    }

    private func infeasibilityStrip(_ infeasibility: Infeasibility, changeset: Changeset) -> some View {
        let copy = TradeOffCopy(time: model.timetable.timeText, context: model.timetable.tradeOffContext(for: changeset))
        return Button {
            showsTradeOffs = true
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                Image(systemName: "exclamationmark.triangle").accessibilityHidden(true)
                Text("\(copy.stripText(infeasibility)) · ").foregroundStyle(ColorToken.warning)
                    + Text("See options").fontWeight(.semibold).foregroundStyle(ColorToken.accent)
                Spacer(minLength: 0)
            }
            .font(.rmt.callout)
            .foregroundStyle(ColorToken.warning)
            .padding(Spacing.m)
            .frame(minHeight: HitTarget.min)
            .background(ColorToken.surfaceRaised, in: RoundedRectangle(cornerRadius: Radius.card))
        }
        .buttonStyle(.plain)
        .accessibilityHint("Shows what doesn't fit and your options")
    }
}

/// Review copy that tests pin down.
enum ProposalReviewCopy {
    /// "Confirm what fits" when the plan is partial (still enabled: rule 3 shows the trade-off, it
    /// doesn't block); otherwise "Confirm changes".
    static func confirmTitle(_ changeset: Changeset) -> String {
        changeset.plan?.infeasibility != nil ? "Confirm what fits" : "Confirm changes"
    }

    static let rejectTitle = "Keep my week"

    /// google_edit with staged pins: rejecting restores our planned time on the edited events (any
    /// already-applied edits in the same batch stay), so the button names the plan and the
    /// footnote says what moves back.
    /// google_edit with empty `staged` (the Google edit is already live, T4.3b): rejecting only
    /// declines the knock-on re-plan and the edit stays, so no footnote.
    static func rejectTitle(_ changeset: Changeset) -> String {
        guard changeset.reason == .googleEdit else { return rejectTitle }
        return changeset.staged.isEmpty ? "Keep the rest of my week" : "Keep original plan"
    }

    /// A non-empty batch may also hold edits that were already applied; those stay.
    static func rejectFootnote(_ changeset: Changeset) -> String? {
        changeset.reason == .googleEdit && !changeset.staged.isEmpty
            ? "Moves the edited events back in Google Calendar. Changes already applied stay as they are." : nil
    }
}

/// diff-and-infeasibility.md §3: "Keep my week" (bordered) + confirm (`borderedProminent`).
struct ConfirmRejectBar: View {
    var rejectTitle = ProposalReviewCopy.rejectTitle
    var rejectFootnote: String?
    let confirmTitle: String
    let isBusy: Bool
    let onReject: () -> Void
    let onConfirm: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        let layout = dynamicTypeSize.isAccessibilitySize ? AnyLayout(VStackLayout(spacing: Spacing.s)) : AnyLayout(HStackLayout(spacing: Spacing.s))
        VStack(alignment: .leading, spacing: Spacing.s) {
            if let rejectFootnote {
                Text(rejectFootnote)
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            layout {
                Button(action: onReject) {
                    Text(rejectTitle).frame(maxWidth: .infinity, minHeight: HitTarget.min)
                }
                .buttonStyle(.bordered)
                Button(action: onConfirm) {
                    Text(confirmTitle).frame(maxWidth: .infinity, minHeight: HitTarget.min)
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .disabled(isBusy)
        .padding(Spacing.l)
        .background(.bar)
    }
}

/// Infeasibility trade-off sheet (diff-and-infeasibility.md §5): violations in plain language,
/// relaxations as choices sorted by what they recover, each confirmed before it's sent.
struct TradeOffSheet: View {
    let infeasibility: Infeasibility
    let copy: TradeOffCopy
    let onTry: (Relaxation) async -> TimetableModel.RelaxationOutcome
    @Environment(\.dismiss) private var dismiss
    @State private var confirming: Relaxation?
    @State private var isSending = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(Array(infeasibility.violations.enumerated()), id: \.offset) { _, violation in
                        let text = copy.violation(violation)
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            Label {
                                Text(text.title).font(.rmt.headline).foregroundStyle(ColorToken.textPrimary)
                            } icon: {
                                Image(systemName: "exclamationmark.triangle").foregroundStyle(ColorToken.warning)
                            }
                            Text(text.explanation)
                                .font(.rmt.body)
                                .foregroundStyle(ColorToken.textSecondary)
                        }
                        .padding(.vertical, Spacing.xs)
                        .accessibilityElement(children: .combine)
                    }
                } header: {
                    Text(copy.stripText(infeasibility))
                        .font(.rmt.numeral)
                        .foregroundStyle(ColorToken.warning)
                        .textCase(nil)
                }
                if !infeasibility.relaxations.isEmpty {
                    Section("Here's what would help") {
                        ForEach(TradeOffCopy.sorted(infeasibility.relaxations), id: \.self) { relaxation in
                            relaxationCard(relaxation)
                        }
                    }
                }
                Section {
                    Button("Leave it as is") { dismiss() }
                        .frame(minHeight: HitTarget.min)
                } footer: {
                    Text("You can still confirm what fits.")
                }
            }
            .navigationTitle("Doesn't all fit")
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium, .large])
    }

    @ViewBuilder
    private func relaxationCard(_ relaxation: Relaxation) -> some View {
        let text = copy.relaxation(relaxation)
        VStack(alignment: .leading, spacing: Spacing.s) {
            Button {
                errorMessage = nil
                withAnimation(Motion.standard) { confirming = confirming == relaxation ? nil : relaxation }
            } label: {
                VStack(alignment: .leading, spacing: Spacing.xs) {
                    Text(text.title).font(.rmt.headline).foregroundStyle(ColorToken.textPrimary)
                    Text(text.detail).font(.rmt.callout).foregroundStyle(ColorToken.textSecondary)
                }
                .frame(maxWidth: .infinity, minHeight: HitTarget.min, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Shows what trying this does")
            if confirming == relaxation {
                Text(TradeOffCopy.tryItConfirmation)
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.textSecondary)
                if let errorMessage {
                    Text(errorMessage).font(.rmt.callout).foregroundStyle(ColorToken.warning)
                }
                HStack(spacing: Spacing.s) {
                    Button {
                        Task { await tryIt(relaxation) }
                    } label: {
                        Text("Try it").frame(maxWidth: .infinity, minHeight: HitTarget.min)
                    }
                    .buttonStyle(.borderedProminent)
                    Button {
                        withAnimation(Motion.standard) { confirming = nil }
                    } label: {
                        Text("Cancel").frame(maxWidth: .infinity, minHeight: HitTarget.min)
                    }
                    .buttonStyle(.bordered)
                }
                .disabled(isSending)
            }
        }
    }

    private func tryIt(_ relaxation: Relaxation) async {
        isSending = true
        defer { isSending = false }
        switch await onTry(relaxation) {
        case .staged:
            dismiss()
        case .ignored:
            errorMessage = "A newer change is already waiting for you."
        case .failed(let message):
            errorMessage = message
        }
    }
}
