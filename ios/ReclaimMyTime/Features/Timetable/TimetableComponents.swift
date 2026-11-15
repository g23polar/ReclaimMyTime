import SwiftUI

extension TimetableModel {
    /// Grid rows for these columns: windows from preferences, widened to fit `spans`.
    func gridMetrics(columns: [DayColumn], spans: [Span], pointsPerHour: CGFloat) -> GridMetrics {
        TimetableGeometry.metrics(
            columns: columns,
            windows: preferences?.schedulableWindows,
            windowTimeZone: windowTimeZone,
            spans: spans,
            calendar: calendar,
            pointsPerHour: pointsPerHour
        )
    }

    /// Outside-window shade per column (none without preferences).
    func outsideBands(columns: [DayColumn], metrics: GridMetrics) -> [[(Double, Double)]] {
        guard let windows = preferences?.schedulableWindows, let zone = windowTimeZone else {
            return columns.map { _ in [] }
        }
        return columns.map { column in
            TimetableGeometry.outsideBands(
                windows: TimetableGeometry.windowMinutes(for: column, windows: windows, windowTimeZone: zone, calendar: calendar),
                metrics: metrics
            )
        }
    }

    func cardModel(for block: Block, palette: ProjectPaletteStore) -> BlockCardModel {
        let classification = classification(for: block)
        return BlockCardModel(
            project: projectName(block.projectId),
            slot: palette.slot(for: block.projectId),
            span: block.span,
            treatment: classification.treatment,
            isPinned: classification.isPinned,
            gcalStatus: block.gcalStatus
        )
    }

    func elapsedFraction(_ block: Block) -> CGFloat {
        let total = block.span.end.timeIntervalSince(block.span.start)
        guard total > 0 else { return 0 }
        _ = clock
        return CGFloat(now().timeIntervalSince(block.span.start) / total)
    }

    var timeText: TimeText { TimeText(calendar: calendar) }
}

/// A live block with its VoiceOver label, context menu and accessibility actions
/// (timetable.md §3–§4). Gestures (Day view drag) are added by the caller.
struct LiveBlockCard: View {
    let block: Block
    var compact: Bool
    var onOpenDay: (() -> Void)?
    var onEditTime: (Block) -> Void
    @Environment(AppModel.self) private var model
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var pulse = false

    var body: some View {
        let timetable = model.timetable
        let classification = timetable.classification(for: block)
        let card = timetable.cardModel(for: block, palette: model.palette)
        BlockView(
            card: card,
            content: BlockContent.content(minutes: TimeText.minutes(block.span), dynamicTypeSize: dynamicTypeSize, compact: compact),
            elapsedFraction: classification.treatment == .inProgress ? timetable.elapsedFraction(block) : 0,
            compact: compact,
            time: timetable.timeText,
            onSyncTap: { timetable.show(.syncFailed) }
        )
        .scaleEffect(pulse ? 1.05 : 1)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(BlockText(time: timetable.timeText).accessibilityLabel(block, project: card.project, classification: classification))
        .modifier(BlockActionsModifier(block: block, classification: classification, onOpenDay: onOpenDay, onEditTime: onEditTime))
        .task(id: timetable.pulseLineages.contains(block.lineageId)) {
            guard timetable.pulseLineages.contains(block.lineageId) else { return }
            withAnimation(Motion.standard) { pulse = true }
            try? await Task.sleep(for: .milliseconds(300))
            withAnimation(Motion.standard) { pulse = false }
        }
    }
}

/// Context menu + VoiceOver actions: Edit time, Delete (future blocks) or Mark done / I skipped
/// this (started blocks).
struct BlockActionsModifier: ViewModifier {
    let block: Block
    let classification: BlockClassification
    var onOpenDay: (() -> Void)?
    let onEditTime: (Block) -> Void
    @Environment(AppModel.self) private var model

    func body(content: Content) -> some View {
        let timetable = model.timetable
        content
            .contextMenu {
                if let onOpenDay {
                    Button("Open day", systemImage: "calendar.day.timeline.left", action: onOpenDay)
                }
                if classification.isFrozen {
                    if classification.offersMarkDone {
                        Button("Mark done", systemImage: "checkmark.circle") { Task { await timetable.markDone(block) } }
                    }
                    if classification.offersSkip {
                        Button("I skipped this", systemImage: "arrow.uturn.forward.circle") { Task { await timetable.skip(block) } }
                    }
                } else {
                    Button("Edit time", systemImage: "clock") { onEditTime(block) }
                    Button("Delete", systemImage: "trash", role: .destructive) { Task { await timetable.delete(block) } }
                }
            }
            .accessibilityActions {
                if classification.isFrozen {
                    if classification.offersMarkDone {
                        Button("Mark done") { Task { await timetable.markDone(block) } }
                    }
                    if classification.offersSkip {
                        Button("I skipped this") { Task { await timetable.skip(block) } }
                    }
                } else {
                    Button("Edit time") { onEditTime(block) }
                    Button("Delete") { Task { await timetable.delete(block) } }
                }
                if let onOpenDay {
                    Button("Open day", action: onOpenDay)
                }
            }
    }
}

/// "Edit time" (context menu / VoiceOver): the non-drag path to the same `edit` write.
struct EditTimeSheet: View {
    let block: Block
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var start: Date
    @State private var end: Date

    init(block: Block) {
        self.block = block
        _start = State(initialValue: block.span.start)
        _end = State(initialValue: block.span.end)
    }

    var body: some View {
        let timetable = model.timetable
        let proposed = Span(start: DragGuard.snap(start), end: DragGuard.snap(end))
        NavigationStack {
            Form {
                Section {
                    DatePicker("Starts", selection: $start, displayedComponents: .hourAndMinute)
                    DatePicker("Ends", selection: $end, displayedComponents: .hourAndMinute)
                } header: {
                    Text(timetable.projectName(block.projectId))
                } footer: {
                    if let proposed {
                        Text("\(timetable.timeText.timeRange(proposed)) · \(TimeText.duration(minutes: TimeText.minutes(proposed))). Nothing changes until you confirm the proposal.")
                    } else {
                        Text("The end has to be after the start.")
                    }
                }
            }
            .navigationTitle("Edit time")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Propose") {
                        guard let proposed else { return }
                        dismiss()
                        Task { await timetable.edit(block, to: proposed) }
                    }
                    .disabled(proposed == nil || proposed == block.span)
                }
            }
        }
        .presentationDetents([.medium])
    }
}

/// Thin informational row under the header (timetable.md §7).
struct TimetableStatusRow: View {
    let systemImage: String
    let text: String
    var tint: Color = ColorToken.textSecondary
    var action: (() -> Void)?

    var body: some View {
        let row = HStack(spacing: Spacing.s) {
            Image(systemName: systemImage).accessibilityHidden(true)
            Text(text).frame(maxWidth: .infinity, alignment: .leading)
            if action != nil {
                Image(systemName: "chevron.right").font(.rmt.blockMeta).accessibilityHidden(true)
            }
        }
        .font(.rmt.callout)
        .foregroundStyle(tint)
        .padding(.horizontal, Spacing.l)
        .padding(.vertical, Spacing.s)
        .frame(minHeight: HitTarget.min)
        .background(ColorToken.surfaceRaised)
        if let action {
            Button(action: action) { row }.buttonStyle(.plain)
        } else {
            row
        }
    }
}

/// Centered empty-state card over a faded grid (timetable.md §7).
struct TimetableEmptyCard: View {
    let title: String
    var message: String?
    var actionTitle: String?
    var action: (() -> Void)?

    var body: some View {
        VStack(spacing: Spacing.m) {
            Text(title)
                .font(.rmt.headline)
                .foregroundStyle(ColorToken.textPrimary)
                .multilineTextAlignment(.center)
            if let message {
                Text(message)
                    .font(.rmt.body)
                    .foregroundStyle(ColorToken.textSecondary)
                    .multilineTextAlignment(.center)
            }
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .buttonStyle(.borderedProminent)
                    .frame(minHeight: HitTarget.min)
            }
        }
        .padding(Spacing.l)
        .frame(maxWidth: 340)
        .background(ColorToken.surface, in: RoundedRectangle(cornerRadius: Radius.card))
        .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.gridLine, lineWidth: 1))
        .padding(Spacing.l)
        .accessibilityElement(children: .contain)
    }
}

/// Transient timetable notice (toast) at the top.
struct TimetableNoticeModifier: ViewModifier {
    @Environment(AppModel.self) private var model

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .top) {
                if let notice = model.timetable.notice {
                    Text(notice.message)
                        .font(.rmt.callout)
                        .foregroundStyle(ColorToken.textPrimary)
                        .padding(Spacing.m)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(ColorToken.surfaceRaised, in: RoundedRectangle(cornerRadius: Radius.card))
                        .overlay(RoundedRectangle(cornerRadius: Radius.card).strokeBorder(ColorToken.gridLine, lineWidth: 1))
                        .padding(.horizontal, Spacing.l)
                        .padding(.top, Spacing.s)
                        .onTapGesture { model.timetable.dismissNotice() }
                        .accessibilityAddTraits(.isButton)
                        .accessibilityHint("Dismisses this message")
                        .transition(.opacity)
                        .task(id: notice) {
                            AccessibilityNotification.Announcement(notice.message).post()
                            try? await Task.sleep(for: .seconds(4))
                            if model.timetable.notice == notice { model.timetable.dismissNotice() }
                        }
                }
            }
            .animation(Motion.standard, value: model.timetable.notice)
    }
}

extension View {
    func timetableNotice() -> some View {
        modifier(TimetableNoticeModifier())
    }
}
