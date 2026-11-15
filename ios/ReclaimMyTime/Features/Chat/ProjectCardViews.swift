import SwiftUI

/// Cards under an agent message (onboarding.md §4 "Project summary card"). One card per project;
/// several projects stack with a single **Looks right** (one changeset, one confirm). Confirmed
/// cards collapse to one-line rows.
struct ProjectCardStackView: View {
    let entry: ChatModel.Entry
    let onEdit: (ProjectCard) -> Void
    @Environment(AppModel.self) private var model
    @State private var confirmedCount = 0

    var body: some View {
        if let changeset = entry.changeset {
            let cards = model.chat.cards(for: entry)
            let lifecycle = model.pending.lifecycle(of: changeset.id)
            VStack(alignment: .leading, spacing: Spacing.s) {
                switch lifecycle {
                case .pending:
                    pendingStack(cards: cards, changesetId: changeset.id)
                case .confirmed:
                    ForEach(cards) { card in
                        ProjectCardResolvedRow(card: card, symbol: "checkmark.circle.fill", tint: ColorToken.positive, status: "confirmed")
                    }
                case .rejected:
                    ForEach(cards) { card in
                        ProjectCardResolvedRow(card: card, symbol: "xmark.circle", tint: ColorToken.textSecondary, status: "not added")
                    }
                case .superseded, .outOfDate:
                    ForEach(cards) { card in
                        ProjectCardResolvedRow(card: card, symbol: "arrow.triangle.2.circlepath", tint: ColorToken.textSecondary,
                                               status: lifecycle == .outOfDate ? "out of date" : "replaced by a newer proposal")
                    }
                }
            }
            .animation(Motion.standard, value: lifecycle)
            .sensoryFeedback(.success, trigger: confirmedCount)
        }
    }

    @ViewBuilder
    private func pendingStack(cards: [ProjectCard], changesetId: UUID) -> some View {
        let isBusy = model.pending.isResolving
        let confirm = {
            Task {
                await model.chat.confirmCards(entryId: entry.id)
                if model.pending.lifecycle(of: changesetId) == .confirmed { confirmedCount += 1 }
            }
        }
        if cards.count == 1, let card = cards.first {
            ProjectSummaryCardView(card: card, slot: model.palette.slot(for: card.id), onEdit: { onEdit(card) },
                                   onConfirm: { _ = confirm() }, isBusy: isBusy)
        } else {
            ForEach(cards) { card in
                ProjectSummaryCardView(card: card, slot: model.palette.slot(for: card.id), onEdit: { onEdit(card) }, isBusy: isBusy)
            }
            Button {
                _ = confirm()
            } label: {
                Text("Looks right").frame(maxWidth: .infinity, minHeight: HitTarget.min)
            }
            .buttonStyle(.borderedProminent)
            .disabled(isBusy)
            .accessibilityHint("Confirms all \(cards.count) projects")
        }
        if let error = model.pending.actionError {
            Text(error)
                .font(.rmt.callout)
                .foregroundStyle(ColorToken.warning)
        }
    }
}

/// One project summary card.
struct ProjectSummaryCardView: View {
    let card: ProjectCard
    let slot: PaletteSlot
    var onEdit: (() -> Void)?
    var onConfirm: (() -> Void)?
    var isBusy = false
    var text = ProjectCardText()
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            VStack(alignment: .leading, spacing: Spacing.xs) {
                header
                if let line = text.goalLine(card) {
                    Text(line)
                        .font(.rmt.body)
                        .foregroundStyle(ColorToken.textPrimary)
                }
                if let line = text.sessionLine(card) {
                    Text(line)
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.textSecondary)
                }
            }
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(text.accessibilityLabel(card))

            if onEdit != nil || onConfirm != nil {
                Divider()
                footer
            }
        }
        .padding(Spacing.l)
        .background(ColorToken.surface, in: RoundedRectangle(cornerRadius: Radius.card))
        .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.gridLine, lineWidth: 1))
        .accessibilityElement(children: .contain)
    }

    private var header: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.xs))
            : AnyLayout(HStackLayout(alignment: .firstTextBaseline, spacing: Spacing.s))
        return layout {
            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                RoundedRectangle(cornerRadius: 3)
                    .fill(slot.fill)
                    .strokeBorder(slot.edge, lineWidth: 1.5)
                    .frame(width: 14, height: 14)
                Text(text.title(card))
                    .font(.rmt.headline)
                    .foregroundStyle(ColorToken.textPrimary)
            }
            if !dynamicTypeSize.isAccessibilitySize { Spacer(minLength: Spacing.s) }
            if let badge = text.badge(card) {
                Text(badge)
                    .font(.rmt.callout.weight(.semibold))
                    .foregroundStyle(slot.ink)
                    .padding(.horizontal, Spacing.s)
                    .padding(.vertical, 2)
                    .background(slot.fill, in: Capsule())
            }
        }
    }

    private var footer: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.s))
            : AnyLayout(HStackLayout(spacing: Spacing.s))
        return layout {
            if let onEdit {
                Button("Edit", action: onEdit)
                    .buttonStyle(.bordered)
                    .frame(minHeight: HitTarget.min)
                    .accessibilityLabel("Edit \(text.title(card))")
            }
            if !dynamicTypeSize.isAccessibilitySize { Spacer(minLength: 0) }
            if let onConfirm {
                Button(action: onConfirm) {
                    Text("Looks right").frame(minHeight: HitTarget.min)
                }
                .buttonStyle(.borderedProminent)
                .accessibilityHint("Confirms \(text.title(card))")
            }
        }
        .disabled(isBusy)
    }
}

/// A resolved card collapsed to one line (`checkmark.circle.fill` + name for confirmed).
struct ProjectCardResolvedRow: View {
    let card: ProjectCard
    let symbol: String
    let tint: Color
    let status: String
    var text = ProjectCardText()

    var body: some View {
        HStack(spacing: Spacing.s) {
            Image(systemName: symbol)
                .foregroundStyle(tint)
            Text(text.title(card))
                .font(.rmt.body)
                .foregroundStyle(status == "confirmed" ? ColorToken.textPrimary : ColorToken.textSecondary)
            if status != "confirmed" {
                Text(status)
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.textSecondary)
            }
        }
        .padding(.vertical, Spacing.xs)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(text.title(card)), \(status)")
    }
}

#Preview("Project card") {
    ProjectSummaryCardView(
        card: ProjectCard(id: UUID(), draft: ProjectDraft(name: "Portfolio site", goal: .deadline(targetDate: LocalDate("2026-12-12")!, estimatedTotalHours: 30), maxBlockMinutes: 120, requirements: ["laptop"])),
        slot: PaletteSlot(0), onEdit: {}, onConfirm: {}
    )
    .padding()
    .background(ColorToken.bg)
}

#Preview("Project card AX5") {
    ProjectSummaryCardView(
        card: ProjectCard(id: UUID(), draft: ProjectDraft(name: "Portfolio site", goal: .deadline(targetDate: LocalDate("2026-12-12")!, estimatedTotalHours: 30), maxBlockMinutes: 120, requirements: ["laptop"])),
        slot: PaletteSlot(0), onEdit: {}, onConfirm: {}
    )
    .padding()
    .environment(\.dynamicTypeSize, .accessibility5)
}
