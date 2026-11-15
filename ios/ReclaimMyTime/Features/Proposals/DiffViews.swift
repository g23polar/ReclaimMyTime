import SwiftUI

/// Diff-mode blocks (diff-and-infeasibility.md §2), positioned on a TimetableCanvas.
struct DiffLayer: View {
    let diff: DiffModel
    let columns: [DayColumn]
    let metrics: GridMetrics
    let columnWidth: CGFloat
    var compact: Bool
    /// False until the open animation runs: moved blocks start at `before`.
    let settled: Bool
    @Environment(AppModel.self) private var model
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        ForEach(diff.items) { item in
            itemView(item)
        }
    }

    private func rects(_ span: Span?) -> [CGRect] {
        guard let span else { return [] }
        return TimetableGeometry.segments(span, columns: columns, calendar: model.timetable.calendar)
            .map { TimetableGeometry.rect($0, metrics: metrics, columnWidth: columnWidth) }
    }

    private func card(_ item: DiffItem, span: Span) -> BlockCardModel {
        let treatment: BlockTreatment = switch item.block?.state {
        case .completed: .completed
        case .skipped: .skipped
        default: .planned
        }
        return BlockCardModel(
            project: model.timetable.projectName(item.projectId),
            slot: model.palette.slot(for: item.projectId),
            span: span,
            treatment: treatment,
            gcalStatus: item.block?.gcalStatus ?? .synced
        )
    }

    private func content(_ span: Span) -> BlockContent {
        BlockContent.content(minutes: TimeText.minutes(span), dynamicTypeSize: dynamicTypeSize, compact: compact)
    }

    @ViewBuilder
    private func itemView(_ item: DiffItem) -> some View {
        let time = model.timetable.timeText
        let label = DiffText(time: time).accessibilityLabel(item, project: model.timetable.projectName(item.projectId), firstWeek: diff.isFirstWeek)
        switch item.kind {
        case .unchanged:
            if let after = item.after {
                ForEach(Array(rects(after).enumerated()), id: \.offset) { _, rect in
                    BlockView(card: card(item, span: after), style: .unchanged, content: content(after), compact: compact, time: time)
                        .placed(in: rect)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(label)
                }
            }
        case .added:
            if let after = item.after {
                ForEach(Array(rects(after).enumerated()), id: \.offset) { _, rect in
                    BlockView(
                        card: card(item, span: after),
                        style: .proposal(badge: diff.showsNewBadges, newLabel: diff.showsNewBadges && !compact),
                        content: content(after),
                        compact: compact,
                        time: time
                    )
                    .placed(in: rect)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(label)
                }
            }
        case .removed:
            if let before = item.before {
                ForEach(Array(rects(before).enumerated()), id: \.offset) { _, rect in
                    BlockView(card: card(item, span: before), style: .ghost(struck: true, opacity: 1), content: .nameOnly, compact: compact, time: time)
                        .placed(in: rect)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(label)
                }
            }
        case .moved:
            if let before = item.before, let after = item.after {
                movedView(item, before: before, after: after, label: label)
            }
        }
    }

    @ViewBuilder
    private func movedView(_ item: DiffItem, before: Span, after: Span, label: String) -> some View {
        let time = model.timetable.timeText
        let beforeRects = rects(before)
        let afterRects = rects(after)
        let caption = DiffText(time: time).resizeLabel(item)
        ForEach(Array(beforeRects.enumerated()), id: \.offset) { _, rect in
            BlockView(card: card(item, span: before), style: .ghost(struck: false, opacity: 0.4), content: .nameOnly, compact: compact, time: time)
                .placed(in: rect)
                .accessibilityHidden(true)
        }
        if !item.isResizeOnly, let from = beforeRects.first, let to = afterRects.first {
            MoveConnector(from: from, to: to)
                .opacity(settled ? 1 : 0)
                .accessibilityHidden(true)
        }
        ForEach(Array(afterRects.enumerated()), id: \.offset) { index, rect in
            let view = BlockView(card: card(item, span: after), style: .proposal(badge: true, newLabel: false), content: content(after), caption: caption, compact: compact, time: time)
            if index == 0, let from = beforeRects.first {
                if reduceMotion {
                    // Reduce Motion: a crossfade from the old spot to the new one; nothing slides.
                    view.placed(in: from).opacity(settled ? 0 : 1).accessibilityHidden(true)
                    view.placed(in: rect).opacity(settled ? 1 : 0)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(label)
                } else {
                    view.placed(in: rect)
                        .offset(x: settled ? 0 : from.minX - rect.minX, y: settled ? 0 : from.minY - rect.minY)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(label)
                }
            } else {
                view.placed(in: rect)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(label)
            }
        }
    }
}

/// Thin curved connector from a moved block's old spot to its new one.
struct MoveConnector: View {
    let from: CGRect
    let to: CGRect

    var body: some View {
        Path { path in
            let start = CGPoint(x: from.midX, y: from.midY)
            let end = CGPoint(x: to.midX, y: to.midY)
            let control = CGPoint(x: (start.x + end.x) / 2 + 24, y: min(start.y, end.y) - 24)
            path.move(to: start)
            path.addQuadCurve(to: end, control: control)
        }
        .stroke(ColorToken.proposal, style: StrokeStyle(lineWidth: 1, lineCap: .round, dash: [3, 3]))
        .allowsHitTesting(false)
    }
}

/// The diff in grid form: headers (tap to zoom to Day) + canvas.
struct DiffGrid: View {
    let diff: DiffModel
    let columns: [DayColumn]
    let pointsPerHour: CGFloat
    let settled: Bool
    var onOpenDay: ((DayColumn) -> Void)?
    @Environment(AppModel.self) private var model

    var body: some View {
        let timetable = model.timetable
        let metrics = timetable.gridMetrics(columns: columns, spans: diff.allSpans, pointsPerHour: pointsPerHour)
        GeometryReader { proxy in
            let columnWidth = max(0, (proxy.size.width - GridLayoutConstants.gutterWidth - (columns.count == 1 ? Spacing.s : 0)) / CGFloat(max(columns.count, 1)))
            VStack(spacing: 0) {
                DayHeaderRow(columns: columns, columnWidth: columnWidth, today: timetable.now(), time: timetable.timeText, onTap: onOpenDay)
                Divider()
                ScrollViewReader { reader in
                    ScrollView {
                        TimetableCanvas(columns: columns, metrics: metrics, columnWidth: columnWidth, showsNowLine: false, showsLiveLayers: false) {
                            DiffLayer(diff: diff, columns: columns, metrics: metrics, columnWidth: columnWidth, compact: columns.count > 1, settled: settled)
                        }
                        .padding(.vertical, Spacing.s)
                    }
                    .onAppear {
                        let firstChange = diff.changes.compactMap(\.displaySpan).map(\.start).min() ?? diff.allSpans.map(\.start).min()
                        if let firstChange, let hour = TimetableGeometry.anchorHour(for: firstChange, columns: columns, calendar: timetable.calendar) {
                            reader.scrollTo("hour-\(max(metrics.startHour, min(hour, metrics.endHour)))", anchor: .top)
                        }
                    }
                }
            }
        }
    }
}

/// The list alternative: changes only, grouped by day (and the VoiceOver default).
struct DiffList: View {
    let diff: DiffModel
    let columns: [DayColumn]
    @Environment(AppModel.self) private var model

    var body: some View {
        let timetable = model.timetable
        let text = DiffText(time: timetable.timeText)
        let groups = diff.changesByDay(columns: columns)
        List {
            if groups.isEmpty {
                Text("No changes to your blocks.")
                    .font(.rmt.body)
                    .foregroundStyle(ColorToken.textSecondary)
            }
            ForEach(groups, id: \.day) { group in
                Section(timetable.timeText.dayTitle(group.day.start)) {
                    ForEach(group.items) { item in
                        let project = timetable.projectName(item.projectId)
                        Label {
                            Text(text.accessibilityLabel(item, project: project, firstWeek: diff.isFirstWeek))
                                .font(.rmt.body)
                                .strikethrough(item.kind == .removed)
                        } icon: {
                            Image(systemName: icon(item.kind))
                                .foregroundStyle(item.kind == .removed ? ColorToken.textSecondary : ColorToken.proposal)
                        }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    private func icon(_ kind: DiffItem.Kind) -> String {
        switch kind {
        case .added: "sparkles"
        case .removed: "minus.circle"
        case .moved: "arrow.right.circle"
        case .unchanged: "circle"
        }
    }
}
