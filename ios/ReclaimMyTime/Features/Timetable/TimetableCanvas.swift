import SwiftUI

/// The shared grid: gutter, background/shade, busy and forbid layers, a caller-supplied block layer,
/// and the now-line. Used by Week, Day, and proposal review.
struct TimetableCanvas<BlockLayer: View>: View {
    let columns: [DayColumn]
    let metrics: GridMetrics
    let columnWidth: CGFloat
    var showsNowLine = true
    var showsLiveLayers = true
    @ViewBuilder let blockLayer: () -> BlockLayer
    @Environment(AppModel.self) private var model

    var body: some View {
        let timetable = model.timetable
        ZStack(alignment: .topLeading) {
            HourGutter(metrics: metrics, time: timetable.timeText)
            ZStack(alignment: .topLeading) {
                GridBackground(
                    columnCount: columns.count,
                    metrics: metrics,
                    columnWidth: columnWidth,
                    outsideBands: timetable.outsideBands(columns: columns, metrics: metrics)
                )
                if showsLiveLayers {
                    if let busy = timetable.busy {
                        busyLayer(busy)
                    }
                    forbidLayer
                }
                blockLayer()
            }
            .padding(.leading, GridLayoutConstants.gutterWidth)
            if showsNowLine {
                TimelineView(.everyMinute) { _ in
                    nowLine
                }
            }
        }
        .frame(width: GridLayoutConstants.gutterWidth + CGFloat(columns.count) * columnWidth, height: metrics.height, alignment: .topLeading)
    }

    private func busyLayer(_ busy: [Span]) -> some View {
        let time = model.timetable.timeText
        return ForEach(busy, id: \.self) { span in
            ForEach(TimetableGeometry.segments(span, columns: columns, calendar: model.timetable.calendar), id: \.self) { segment in
                BusyView(showsGlyph: TimeText.minutes(span) >= 30)
                    .placed(in: TimetableGeometry.rect(segment, metrics: metrics, columnWidth: columnWidth))
                    .accessibilityElement()
                    .accessibilityLabel("Busy, \(time.timeRange(span))")
            }
        }
    }

    private var forbidLayer: some View {
        let timetable = model.timetable
        return ForEach(timetable.forbids) { pin in
            ForEach(TimetableGeometry.segments(pin.span, columns: columns, calendar: timetable.calendar), id: \.self) { segment in
                ForbidView()
                    .placed(in: TimetableGeometry.rect(segment, metrics: metrics, columnWidth: columnWidth))
                    .accessibilityElement()
                    .accessibilityLabel("Kept free, \(timetable.timeText.timeRange(pin.span))")
            }
        }
    }

    @ViewBuilder
    private var nowLine: some View {
        let timetable = model.timetable
        let now = timetable.now()
        if let index = columns.firstIndex(where: { $0.contains(now) }) {
            let minute = TimetableGeometry.minuteOfDay(now, in: columns[index], calendar: timetable.calendar)
            if minute >= Double(metrics.startHour * 60), minute <= Double(metrics.endHour * 60) {
                NowLine(
                    y: metrics.y(minuteOfDay: minute),
                    columnX: CGFloat(index) * columnWidth,
                    columnWidth: columnWidth,
                    label: timetable.timeText.time(now)
                )
            }
        }
    }
}

/// Live blocks as static cards (Week view; Day view adds gestures itself). A block with a staged
/// edit is drawn as a faint ghost at its live spot plus the proposal (timetable.md §5).
struct LiveBlocksLayer: View {
    let blocks: [Block]
    let columns: [DayColumn]
    let metrics: GridMetrics
    let columnWidth: CGFloat
    var compact: Bool
    var onOpenDay: ((Block) -> Void)?
    let onEditTime: (Block) -> Void
    @Environment(AppModel.self) private var model

    var body: some View {
        let timetable = model.timetable
        let staged = timetable.stagedEdits
        ForEach(blocks) { block in
            let segments = TimetableGeometry.segments(block.span, columns: columns, calendar: timetable.calendar)
            ForEach(segments, id: \.self) { segment in
                let rect = TimetableGeometry.rect(segment, metrics: metrics, columnWidth: columnWidth)
                if staged[block.id] != nil {
                    BlockView(card: timetable.cardModel(for: block, palette: model.palette), style: .ghost(struck: false, opacity: 0.4), content: .nameOnly, compact: compact, time: timetable.timeText)
                        .placed(in: rect)
                        .accessibilityHidden(true)
                } else {
                    LiveBlockCard(block: block, compact: compact, onOpenDay: onOpenDay.map { open in { open(block) } }, onEditTime: onEditTime)
                        .placed(in: rect)
                        .onTapGesture { onOpenDay?(block) }
                }
            }
            if let edit = staged[block.id] {
                StagedEditView(block: block, edit: edit, columns: columns, metrics: metrics, columnWidth: columnWidth, compact: compact)
            }
        }
    }
}

/// A staged edit drawn as a proposal: the moved block (`proposal` outline + `sparkles`) or, for a
/// delete, the forbid it creates.
struct StagedEditView: View {
    let block: Block
    let edit: StagedBlockEdit
    let columns: [DayColumn]
    let metrics: GridMetrics
    let columnWidth: CGFloat
    var compact: Bool
    @Environment(AppModel.self) private var model
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        let timetable = model.timetable
        let time = timetable.timeText
        let base = timetable.cardModel(for: block, palette: model.palette)
        switch edit {
        case .moved(let span):
            let card = BlockCardModel(project: base.project, slot: base.slot, span: span, treatment: .planned, isPinned: false, gcalStatus: .synced)
            ForEach(TimetableGeometry.segments(span, columns: columns, calendar: timetable.calendar), id: \.self) { segment in
                BlockView(
                    card: card,
                    style: .proposal(badge: true, newLabel: false),
                    content: BlockContent.content(minutes: TimeText.minutes(span), dynamicTypeSize: dynamicTypeSize, compact: compact),
                    compact: compact,
                    time: time
                )
                .placed(in: TimetableGeometry.rect(segment, metrics: metrics, columnWidth: columnWidth))
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Proposed: \(base.project), \(time.spokenSpan(span)). Waiting for you to confirm.")
                .onTapGesture { model.navigation.presentReview() }
            }
        case .deleted(let span):
            ForEach(TimetableGeometry.segments(span, columns: columns, calendar: timetable.calendar), id: \.self) { segment in
                ForbidView(isProposal: true)
                    .placed(in: TimetableGeometry.rect(segment, metrics: metrics, columnWidth: columnWidth))
                    .accessibilityElement()
                    .accessibilityLabel("Proposed: keep \(time.timeRange(span)) free. Waiting for you to confirm.")
                    .onTapGesture { model.navigation.presentReview() }
            }
        }
    }
}
