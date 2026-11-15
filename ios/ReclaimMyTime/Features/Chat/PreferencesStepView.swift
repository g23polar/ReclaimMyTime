import SwiftUI

/// The in-chat preferences step (onboarding.md §4): schedulable-hours card + focused/mixed cards +
/// submit. Questions render as static agent bubbles (client copy, no LLM call).
struct PreferencesStepView: View {
    @Bindable var form: PreferencesFormModel
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.m) {
            AgentBubble(text: ChatModel.preferencesQuestionHours, state: .done)
            SchedulableHoursCard(form: form)
            AgentBubble(text: ChatModel.preferencesQuestionDiversity, state: .done)
            DayDiversityPicker(selection: $form.diversity)
                .disabled(form.phase != .editing)
            if form.phase == .submitted {
                Label("Sent. Review your first week to confirm it.", systemImage: "checkmark.circle.fill")
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.positive)
            } else {
                if let issue = form.validationIssue, issue != .noDiversity || form.diversity == nil {
                    Text(issue.message)
                        .font(.rmt.callout)
                        .foregroundStyle(issue == .noDiversity ? ColorToken.textSecondary : ColorToken.warning)
                }
                if let error = form.submitError {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.warning)
                }
                Button {
                    Task { await model.chat.submitPreferences() }
                } label: {
                    Group {
                        if form.phase == .submitting { ProgressView() } else { Text("Plan my week") }
                    }
                    .frame(maxWidth: .infinity, minHeight: HitTarget.min)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!form.canSubmit)
            }
        }
    }
}

/// "When can I schedule things?": rows of day group + two compact time pickers.
struct SchedulableHoursCard: View {
    @Bindable var form: PreferencesFormModel

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.s) {
            ForEach($form.rows) { $row in
                WindowRowView(row: $row, canRemove: form.rows.count > 1) { form.removeRow(row.id) }
                if row.id != form.rows.last?.id { Divider() }
            }
            Button {
                form.addRow()
            } label: {
                Label("Add another window", systemImage: "plus")
                    .frame(minHeight: HitTarget.min)
            }
        }
        .disabled(form.phase != .editing)
        .padding(Spacing.l)
        .background(ColorToken.surface, in: RoundedRectangle(cornerRadius: Radius.card))
        .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.gridLine, lineWidth: 1))
    }
}

struct WindowRowView: View {
    @Binding var row: PreferencesFormModel.WindowRow
    let canRemove: Bool
    let onRemove: () -> Void
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        // One line when it fits; otherwise days + remove on top, times below. At AX sizes the
        // times stack too.
        ViewThatFits(in: .horizontal) {
            HStack(spacing: Spacing.xs) {
                dayPicker
                Spacer(minLength: 0)
                times
                removeButton
            }
            VStack(alignment: .leading, spacing: Spacing.xs) {
                HStack {
                    dayPicker
                    Spacer(minLength: 0)
                    removeButton
                }
                times
            }
        }
    }

    private var dayPicker: some View {
        Picker("Days", selection: $row.days) {
            ForEach(PreferencesFormModel.DayGroup.allCases) { group in
                Text(group.label).tag(group)
            }
        }
        .pickerStyle(.menu)
        .labelsHidden()
        .fixedSize()
    }

    @ViewBuilder
    private var times: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: Spacing.xs))
            : AnyLayout(HStackLayout(spacing: Spacing.xs))
        layout {
            DatePicker("From", selection: minutesBinding(\.startMinutes), displayedComponents: .hourAndMinute)
                .labelsHidden()
                .datePickerStyle(.compact)
                .accessibilityLabel("\(row.days.label) from")
            if !dynamicTypeSize.isAccessibilitySize {
                Text("–").accessibilityHidden(true)
            }
            DatePicker("To", selection: minutesBinding(\.endMinutes), displayedComponents: .hourAndMinute)
                .labelsHidden()
                .datePickerStyle(.compact)
                .accessibilityLabel("\(row.days.label) until")
        }
        .fixedSize()
    }

    @ViewBuilder
    private var removeButton: some View {
        if canRemove {
            Button(role: .destructive, action: onRemove) {
                Image(systemName: "minus.circle")
                    .frame(minWidth: HitTarget.min, minHeight: HitTarget.min)
            }
            .buttonStyle(.borderless)
            .accessibilityLabel("Remove \(row.days.label) window")
        }
    }

    /// Bridges minutes-after-midnight to the DatePicker's `Date` on today's local calendar. Input
    /// only: the stored value is wall-clock minutes, sent as "HH:MM".
    private func minutesBinding(_ keyPath: WritableKeyPath<PreferencesFormModel.WindowRow, Int>) -> Binding<Date> {
        Binding(
            get: {
                let calendar = Calendar.current
                let midnight = calendar.startOfDay(for: Date())
                let minutes = row[keyPath: keyPath]
                return calendar.date(bySettingHour: minutes / 60, minute: minutes % 60, second: 0, of: midnight) ?? midnight
            },
            set: { date in
                let parts = Calendar.current.dateComponents([.hour, .minute], from: date)
                row[keyPath: keyPath] = (parts.hour ?? 0) * 60 + (parts.minute ?? 0)
            }
        )
    }
}

/// Themed days (`focused`) vs variety (`mixed`): two large selectable cards, stacked at AX sizes.
struct DayDiversityPicker: View {
    @Binding var selection: DayDiversity?
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    static func stacks(at size: DynamicTypeSize) -> Bool { size.isAccessibilitySize }

    var body: some View {
        let layout = Self.stacks(at: dynamicTypeSize)
            ? AnyLayout(VStackLayout(spacing: Spacing.m))
            : AnyLayout(HStackLayout(alignment: .top, spacing: Spacing.m))
        layout {
            option(.focused, title: "Themed days", caption: "One project gets the day.", colors: [PaletteSlot(0), PaletteSlot(0), PaletteSlot(0), PaletteSlot(0)])
            option(.mixed, title: "Variety", caption: "A bit of everything, every day.", colors: [PaletteSlot(0), PaletteSlot(1), PaletteSlot(2), PaletteSlot(1)])
        }
    }

    private func option(_ value: DayDiversity, title: String, caption: String, colors: [PaletteSlot]) -> some View {
        let isSelected = selection == value
        return Button {
            selection = value
        } label: {
            VStack(alignment: .leading, spacing: Spacing.s) {
                HStack(alignment: .top) {
                    DayColumnIllustration(slots: colors)
                    Spacer()
                    Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(isSelected ? ColorToken.accent : ColorToken.textSecondary)
                        .font(.title3)
                }
                Text(title)
                    .font(.rmt.headline)
                    .foregroundStyle(ColorToken.textPrimary)
                Text(caption)
                    .font(.rmt.callout)
                    .foregroundStyle(ColorToken.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(Spacing.l)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(ColorToken.surface, in: RoundedRectangle(cornerRadius: Radius.card))
            .overlay(
                RoundedRectangle(cornerRadius: Radius.card)
                    .strokeBorder(isSelected ? ColorToken.accent : ColorToken.gridLine, lineWidth: isSelected ? 2 : 1)
            )
            .contentShape(RoundedRectangle(cornerRadius: Radius.card))
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title). \(caption)")
        .accessibilityAddTraits(isSelected ? [.isButton, .isSelected] : .isButton)
    }
}

/// Decorative mini day column: one color (themed) or several (variety).
struct DayColumnIllustration: View {
    let slots: [PaletteSlot]

    var body: some View {
        VStack(spacing: 2) {
            ForEach(Array(slots.enumerated()), id: \.offset) { _, slot in
                RoundedRectangle(cornerRadius: 2)
                    .fill(slot.fill)
                    .overlay(alignment: .leading) { Rectangle().fill(slot.edge).frame(width: 2) }
                    .frame(width: 28, height: 9)
            }
        }
        .padding(3)
        .background(ColorToken.surfaceRaised, in: RoundedRectangle(cornerRadius: 4))
        .accessibilityHidden(true)
    }
}

#Preview("Preferences cards AX5") {
    ScrollView {
        VStack {
            SchedulableHoursCard(form: PreferencesFormModel())
            DayDiversityPicker(selection: .constant(.mixed))
        }
        .padding()
    }
    .environment(\.dynamicTypeSize, .accessibility5)
}
