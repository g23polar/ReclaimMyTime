import SwiftUI

/// Day view (timetable.md §2, §5): one column at 64 pt per hour, full block anatomy, and the
/// editing surface. Long-press (0.3 s) lifts a future block; drag moves it, the handles resize it,
/// the trash target deletes it. Every release is a staged write; nothing moves in the live plan.
struct DayView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var day: Date
    @State private var selected: UUID?
    @State private var drag: ActiveDrag?
    @State private var trashFrame: CGRect = .zero
    @State private var editing: Block?
    @State private var suppressSwipe = false
    @GestureState private var gestureActive = false

    private static let space = "dayCanvas"

    struct ActiveDrag: Equatable {
        let block: Block
        var handle: DragGuard.Handle
        var proposed: Span
        var overTrash: Bool
    }

    init(initialDay: Date) {
        _day = State(initialValue: initialDay)
    }

    var body: some View {
        let timetable = model.timetable
        let weekColumns = timetable.columns(weekOffset: 0)
        let column = TimetableGeometry.columns(from: day, calendar: timetable.calendar, count: 1)
        let blocks = (timetable.plan?.blocks ?? []).filter { block in column.contains { block.span.start < $0.end && block.span.end > $0.start } }
        let metrics = timetable.gridMetrics(columns: column, spans: blocks.map(\.span), pointsPerHour: GridMetrics.dayPointsPerHour)
        let index = weekColumns.firstIndex { $0.start == column.first?.start }

        GeometryReader { proxy in
            let columnWidth = max(0, proxy.size.width - GridLayoutConstants.gutterWidth - Spacing.s)
            ScrollViewReader { reader in
                ScrollView {
                    TimetableCanvas(columns: column, metrics: metrics, columnWidth: columnWidth) {
                        blockLayer(blocks: blocks, column: column, metrics: metrics, columnWidth: columnWidth)
                    }
                    .padding(.vertical, Spacing.s)
                    .contentShape(Rectangle())
                    .onTapGesture { selected = nil }
                }
                .scrollDisabled(drag != nil)
                .onAppear { scrollToAnchor(reader, blocks: blocks, column: column, metrics: metrics) }
            }
        }
        .coordinateSpace(name: Self.space)
        .overlay(alignment: .bottom) { trashTarget }
        .simultaneousGesture(swipe(index: index, columns: weekColumns))
        .background(ColorToken.bg)
        .navigationTitle(timetable.timeText.dayTitle(day))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Button {
                    if let index, index > 0 { change(to: weekColumns[index - 1].start) }
                } label: {
                    Label("Previous day", systemImage: "chevron.left")
                }
                .disabled((index ?? 0) <= 0)
                Button {
                    if let index, index + 1 < weekColumns.count { change(to: weekColumns[index + 1].start) }
                } label: {
                    Label("Next day", systemImage: "chevron.right")
                }
                .disabled((index ?? weekColumns.count) >= weekColumns.count - 1)
            }
        }
        .timetableNotice()
        .sheet(item: $editing) { EditTimeSheet(block: $0) }
        .sensoryFeedback(.impact(weight: .light), trigger: drag.map { DragGuard.tick($0.proposed, handle: $0.handle) })
        .sensoryFeedback(.impact(flexibility: .rigid), trigger: timetable.rejectedDropCount)
        .sensoryFeedback(.selection, trigger: selected)
        .onChange(of: gestureActive) { _, active in
            // A cancelled gesture never calls onEnded: drop the lifted state.
            if !active, drag != nil { drag = nil }
        }
    }

    // MARK: layers

    @ViewBuilder
    private func blockLayer(blocks: [Block], column: [DayColumn], metrics: GridMetrics, columnWidth: CGFloat) -> some View {
        let timetable = model.timetable
        let staged = timetable.stagedEdits
        ForEach(blocks) { block in
            let classification = timetable.classification(for: block)
            let isDragging = drag?.block.id == block.id
            ForEach(TimetableGeometry.segments(block.span, columns: column, calendar: timetable.calendar), id: \.self) { segment in
                let rect = TimetableGeometry.rect(segment, metrics: metrics, columnWidth: columnWidth)
                if staged[block.id] != nil || isDragging {
                    BlockView(card: timetable.cardModel(for: block, palette: model.palette), style: .ghost(struck: false, opacity: 0.4), content: .nameOnly, time: timetable.timeText)
                        .placed(in: rect)
                        .accessibilityHidden(true)
                } else {
                    LiveBlockCard(block: block, compact: false, onEditTime: { editing = $0 })
                        .scaleEffect(selected == block.id ? 1.03 : 1)
                        .shadow(color: .black.opacity(selected == block.id ? 0.18 : 0), radius: 6, y: 3)
                        .animation(Motion.quick, value: selected)
                        .gesture(bodyGesture(block), including: DragGuard.canPickUp(classification) ? .all : .subviews)
                        .placed(in: rect)
                    if selected == block.id, DragGuard.canPickUp(classification), drag == nil {
                        handles(block, rect: rect)
                    }
                }
            }
            if let edit = staged[block.id], !isDragging {
                StagedEditView(block: block, edit: edit, columns: column, metrics: metrics, columnWidth: columnWidth, compact: false)
            }
        }
        if let drag {
            dragPreview(drag, column: column, metrics: metrics, columnWidth: columnWidth)
        }
    }

    @ViewBuilder
    private func dragPreview(_ drag: ActiveDrag, column: [DayColumn], metrics: GridMetrics, columnWidth: CGFloat) -> some View {
        let timetable = model.timetable
        let base = timetable.cardModel(for: drag.block, palette: model.palette)
        let card = BlockCardModel(project: base.project, slot: base.slot, span: drag.proposed, treatment: .planned, isPinned: base.isPinned, gcalStatus: .synced)
        if let segment = TimetableGeometry.segments(drag.proposed, columns: column, calendar: timetable.calendar).first {
            let rect = TimetableGeometry.rect(segment, metrics: metrics, columnWidth: columnWidth)
            BlockView(card: card, content: .nameOnly, time: timetable.timeText)
                .scaleEffect(1.03)
                .shadow(color: .black.opacity(0.22), radius: 8, y: 4)
                .opacity(drag.overTrash ? 0.5 : 1)
                .placed(in: rect)
                .allowsHitTesting(false)
            Text(tooltip(drag))
                .font(.rmt.blockMeta)
                .foregroundStyle(ColorToken.onAccent)
                .padding(.horizontal, Spacing.s)
                .padding(.vertical, Spacing.xs)
                .background(ColorToken.accent, in: Capsule())
                .offset(x: rect.minX + Spacing.s, y: max(0, rect.minY - 26))
                .allowsHitTesting(false)
                .accessibilityHidden(true)
        }
    }

    private func tooltip(_ drag: ActiveDrag) -> String {
        let time = model.timetable.timeText
        switch drag.handle {
        case .body: return time.timeRange(drag.proposed)
        case .top, .bottom: return "\(time.timeRange(drag.proposed)) · \(TimeText.duration(minutes: TimeText.minutes(drag.proposed)))"
        }
    }

    /// Top/bottom resize handles: drawn small, hit area 44 pt (tokens.md `hit.min`).
    private func handles(_ block: Block, rect: CGRect) -> some View {
        ZStack(alignment: .topLeading) {
            handle(block, edge: .top)
                .offset(x: rect.minX + rect.width / 2 - HitTarget.min / 2, y: rect.minY - HitTarget.min / 2)
            handle(block, edge: .bottom)
                .offset(x: rect.minX + rect.width / 2 - HitTarget.min / 2, y: rect.maxY - HitTarget.min / 2)
        }
    }

    private func handle(_ block: Block, edge: DragGuard.Handle) -> some View {
        Capsule()
            .fill(ColorToken.accent)
            .frame(width: 28, height: 6)
            .overlay(Capsule().strokeBorder(ColorToken.surface, lineWidth: 1))
            .frame(width: HitTarget.min, height: HitTarget.min)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0, coordinateSpace: .named(Self.space))
                    .updating($gestureActive) { _, state, _ in state = true }
                    .onChanged { value in update(block, handle: edge, translation: value.translation.height, location: value.location) }
                    .onEnded { _ in finish() }
            )
            .accessibilityHidden(true)
    }

    @ViewBuilder
    private var trashTarget: some View {
        if let drag, drag.handle == .body {
            Image(systemName: drag.overTrash ? "trash.fill" : "trash")
                .font(.title2)
                .foregroundStyle(drag.overTrash ? ColorToken.onAccent : ColorToken.destructive)
                .frame(width: 64, height: 64)
                .background(drag.overTrash ? ColorToken.destructive : ColorToken.surfaceRaised, in: Circle())
                .overlay(Circle().strokeBorder(ColorToken.destructive, lineWidth: 1.5))
                .scaleEffect(drag.overTrash ? 1.1 : 1)
                .animation(Motion.quick, value: drag.overTrash)
                .background {
                    GeometryReader { proxy in
                        Color.clear
                            .onAppear { trashFrame = proxy.frame(in: .named(Self.space)) }
                            .onChange(of: proxy.frame(in: .named(Self.space))) { _, frame in trashFrame = frame }
                    }
                }
                .padding(.bottom, Spacing.xl)
                .transition(.opacity)
                .accessibilityHidden(true)
        }
    }

    // MARK: gestures

    private func bodyGesture(_ block: Block) -> some Gesture {
        LongPressGesture(minimumDuration: 0.3)
            .sequenced(before: DragGesture(minimumDistance: 0, coordinateSpace: .named(Self.space)))
            .updating($gestureActive) { _, state, _ in state = true }
            .onChanged { value in
                switch value {
                case .first(true):
                    selected = block.id
                case .second(true, let dragValue?):
                    suppressSwipe = true
                    update(block, handle: .body, translation: dragValue.translation.height, location: dragValue.location)
                default:
                    break
                }
            }
            .onEnded { value in
                if case .second(true, _?) = value { finish() } else { drag = nil }
            }
    }

    private func update(_ block: Block, handle: DragGuard.Handle, translation: CGFloat, location: CGPoint) {
        let delta = Double(translation / GridMetrics.dayPointsPerHour * 60)
        let proposed = DragGuard.proposedSpan(original: block.span, handle: handle, deltaMinutes: delta, bounds: bounds(for: block))
        suppressSwipe = true
        drag = ActiveDrag(
            block: block,
            handle: handle,
            proposed: proposed,
            overTrash: handle == .body && trashFrame.insetBy(dx: -12, dy: -12).contains(location)
        )
    }

    private func finish() {
        let timetable = model.timetable
        guard let current = drag else { return }
        drag = nil
        releaseSwipeSuppression()
        if current.overTrash {
            selected = nil
            Task { await timetable.delete(current.block) }
            return
        }
        switch DragGuard.decide(original: current.block.span, proposed: current.proposed, now: timetable.now()) {
        case .send(let span):
            selected = nil
            Task { await timetable.edit(current.block, to: span) }
        case .rejectedPast:
            timetable.rejectPastDrop()
        case .unchanged:
            break
        }
    }

    /// The block stays on its day and inside the plan horizon (D7: no cross-week drag).
    private func bounds(for block: Block) -> Span {
        let timetable = model.timetable
        let column = TimetableGeometry.columns(from: day, calendar: timetable.calendar, count: 1).first
        var start = column?.start ?? block.span.start
        var end = column?.end ?? block.span.end
        if let horizon = timetable.plan?.horizon {
            start = max(start, horizon.start)
            end = min(end, horizon.end)
        }
        return Span(start: start, end: end) ?? block.span
    }

    private func swipe(index: Int?, columns: [DayColumn]) -> some Gesture {
        DragGesture(minimumDistance: 40)
            .onEnded { value in
                guard drag == nil, !suppressSwipe, let index else { return }
                let dx = value.translation.width, dy = value.translation.height
                guard abs(dx) > 80, abs(dx) > abs(dy) * 1.5 else { return }
                if dx < 0, index + 1 < columns.count { change(to: columns[index + 1].start) }
                if dx > 0, index > 0 { change(to: columns[index - 1].start) }
            }
    }

    private func releaseSwipeSuppression() {
        Task {
            try? await Task.sleep(for: .milliseconds(300))
            suppressSwipe = false
        }
    }

    private func change(to newDay: Date) {
        selected = nil
        withAnimation(reduceMotion ? nil : Motion.standard) { day = newDay }
    }

    private func scrollToAnchor(_ reader: ScrollViewProxy, blocks: [Block], column: [DayColumn], metrics: GridMetrics) {
        let timetable = model.timetable
        let now = timetable.now()
        let anchor = column.first?.contains(now) == true ? now : blocks.map(\.span.start).min()
        guard let anchor, let hour = TimetableGeometry.anchorHour(for: anchor, columns: column, calendar: timetable.calendar) else { return }
        reader.scrollTo("hour-\(max(metrics.startHour, min(hour, metrics.endHour)))", anchor: .top)
    }
}
