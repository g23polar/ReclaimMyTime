import SwiftUI

struct DayRoute: Hashable {
    let day: Date
}

/// Week tab root (timetable.md §1): 7-column grid, 44 pt per hour; list of days at AX sizes.
/// Long-press context menus only; dragging lives in Day view.
struct WeekView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var weekOffset = 0
    @State private var path: [DayRoute] = []
    @State private var editing: Block?

    var body: some View {
        let timetable = model.timetable
        let columns = TimetableGeometry.columns(from: timetable.weekStart(offset: weekOffset), calendar: timetable.calendar)
        NavigationStack(path: $path) {
            VStack(spacing: 0) {
                statusRows
                content(columns: columns)
            }
            .background(ColorToken.bg)
            .navigationTitle(title(columns))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        weekOffset -= 1
                    } label: {
                        Label("Previous week", systemImage: "chevron.left")
                    }
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button("Today") { weekOffset = 0 }
                        .disabled(weekOffset == 0)
                    Button {
                        weekOffset += 1
                    } label: {
                        Label("Next week", systemImage: "chevron.right")
                    }
                }
            }
            .navigationDestination(for: DayRoute.self) { route in
                DayView(initialDay: route.day)
            }
        }
        .timetableNotice()
        .sheet(item: $editing) { EditTimeSheet(block: $0) }
        .task(id: model.pending.liveStateRevision) { await timetable.load() }
        .task { await model.calendar.refreshStatus() }
        .onChange(of: timetable.requestedDay, initial: true) { _, day in
            guard let day else { return }
            weekOffset = 0
            path = [DayRoute(day: day)]
            timetable.requestedDay = nil
        }
        .task(id: timetable.pulseLineages) {
            guard !timetable.pulseLineages.isEmpty else { return }
            try? await Task.sleep(for: .seconds(2))
            timetable.clearPulse()
        }
    }

    private func title(_ columns: [DayColumn]) -> String {
        guard let first = columns.first, let last = columns.last else { return "Week" }
        return model.timetable.timeText.weekRange(first: first.start, last: last.start)
    }

    @ViewBuilder
    private var statusRows: some View {
        let timetable = model.timetable
        if model.calendar.status == .notConnected || model.calendar.status == .revoked {
            TimetableStatusRow(systemImage: "calendar", text: "Calendar not connected: planned without your meetings.") {
                model.navigation.selectedTab = .settings
            }
            .accessibilityHint("Opens Settings")
        }
        if timetable.plan != nil {
            switch timetable.connectivity {
            case .online:
                EmptyView()
            case .offline:
                TimetableStatusRow(systemImage: "wifi.slash", text: "Offline. Showing your last saved week.")
            case .error:
                TimetableStatusRow(systemImage: "exclamationmark.triangle", text: "Couldn't refresh. Showing your last saved week.", tint: ColorToken.warning)
            }
        }
    }

    @ViewBuilder
    private func content(columns: [DayColumn]) -> some View {
        let timetable = model.timetable
        let blocks = timetable.blocks(weekOffset: weekOffset)
        switch timetable.phase {
        case .idle, .loading:
            WeekGrid(columns: columns, blocks: [], placeholder: true, onOpenDay: open, onEditTime: { editing = $0 })
        case .unavailable:
            faded(columns) {
                TimetableEmptyCard(
                    title: timetable.connectivity == .offline ? "You're offline." : "Couldn't load your week.",
                    message: "Nothing changed. Try again in a moment.",
                    actionTitle: "Try again",
                    action: { Task { await timetable.load() } }
                )
            }
        case .noPlan:
            faded(columns) {
                TimetableEmptyCard(title: "No week planned yet.", actionTitle: weekOffset == 0 ? "Plan my week" : nil, action: planMyWeek)
            }
        case .loaded:
            if weekOffset != 0 {
                faded(columns) { TimetableEmptyCard(title: "No week planned yet.") }
            } else if TimetableLayoutMode.mode(for: dynamicTypeSize) == .list {
                if blocks.isEmpty {
                    TimetableEmptyCard(title: "Nothing scheduled this week. Add a project in Chat to get started.")
                        .frame(maxHeight: .infinity, alignment: .top)
                } else {
                    WeekListView(columns: columns, blocks: blocks, onEditTime: { editing = $0 })
                }
            } else {
                WeekGrid(columns: columns, blocks: blocks, onOpenDay: open, onEditTime: { editing = $0 })
                    .overlay {
                        if blocks.isEmpty {
                            TimetableEmptyCard(title: "Nothing scheduled this week. Add a project in Chat to get started.")
                        }
                    }
            }
        }
    }

    private func faded<Card: View>(_ columns: [DayColumn], @ViewBuilder card: () -> Card) -> some View {
        WeekGrid(columns: columns, blocks: [], onOpenDay: nil, onEditTime: { _ in })
            .opacity(0.4)
            .accessibilityHidden(true)
            .overlay { card() }
    }

    private func open(_ column: DayColumn) {
        path.append(DayRoute(day: column.start))
    }

    private func planMyWeek() {
        model.navigation.selectedTab = .chat
    }
}

/// The 7-column grid with headers, scrolled to the now-line (or the first block).
struct WeekGrid: View {
    let columns: [DayColumn]
    let blocks: [Block]
    var placeholder = false
    let onOpenDay: ((DayColumn) -> Void)?
    let onEditTime: (Block) -> Void
    @Environment(AppModel.self) private var model

    var body: some View {
        let timetable = model.timetable
        let metrics = timetable.gridMetrics(columns: columns, spans: blocks.map(\.span), pointsPerHour: GridMetrics.weekPointsPerHour)
        GeometryReader { proxy in
            let columnWidth = max(0, (proxy.size.width - GridLayoutConstants.gutterWidth) / CGFloat(max(columns.count, 1)))
            VStack(spacing: 0) {
                DayHeaderRow(columns: columns, columnWidth: columnWidth, today: timetable.now(), time: timetable.timeText, onTap: onOpenDay)
                Divider()
                ScrollViewReader { reader in
                    ScrollView {
                        TimetableCanvas(columns: columns, metrics: metrics, columnWidth: columnWidth, showsLiveLayers: !placeholder) {
                            if placeholder {
                                PlaceholderBlocks(columns: columns, metrics: metrics, columnWidth: columnWidth)
                            } else {
                                LiveBlocksLayer(
                                    blocks: blocks,
                                    columns: columns,
                                    metrics: metrics,
                                    columnWidth: columnWidth,
                                    compact: true,
                                    onOpenDay: onOpenDay.map { open in
                                        { block in
                                            if let column = columns.first(where: { $0.contains(block.span.start) }) { open(column) }
                                        }
                                    },
                                    onEditTime: onEditTime
                                )
                            }
                        }
                        .padding(.vertical, Spacing.s)
                    }
                    .onAppear { scrollToAnchor(reader, metrics: metrics) }
                }
            }
        }
    }

    private func scrollToAnchor(_ reader: ScrollViewProxy, metrics: GridMetrics) {
        let timetable = model.timetable
        let now = timetable.now()
        let anchor: Date? = columns.contains(where: { $0.contains(now) }) ? now : blocks.map(\.span.start).min()
        guard let anchor else { return }
        let hour = timetable.calendar.component(.hour, from: anchor) - 1
        reader.scrollTo("hour-\(max(metrics.startHour, min(hour, metrics.endHour)))", anchor: .top)
    }
}

/// Loading skeleton: redacted block shapes (timetable.md §7).
struct PlaceholderBlocks: View {
    let columns: [DayColumn]
    let metrics: GridMetrics
    let columnWidth: CGFloat

    var body: some View {
        ForEach(Array(columns.indices), id: \.self) { index in
            let start = Double(metrics.startHour * 60) + Double((index * 97) % 300) + 60
            let rect = TimetableGeometry.rect(
                ColumnSegment(column: index, startMinute: start, endMinute: start + 60 + Double(index % 3) * 30),
                metrics: metrics,
                columnWidth: columnWidth
            )
            RoundedRectangle(cornerRadius: Radius.block)
                .fill(ColorToken.surfaceRaised)
                .overlay(alignment: .topLeading) {
                    Text("Project").font(.rmt.blockTitle).padding(Spacing.xs)
                }
                .redacted(reason: .placeholder)
                .placed(in: rect)
        }
        .accessibilityHidden(true)
    }
}

/// AX sizes (`.accessibility1`+): a list grouped by day instead of the grid (timetable.md §1).
struct WeekListView: View {
    let columns: [DayColumn]
    let blocks: [Block]
    let onEditTime: (Block) -> Void
    @Environment(AppModel.self) private var model

    var body: some View {
        let timetable = model.timetable
        let text = BlockText(time: timetable.timeText)
        let staged = timetable.stagedEdits
        List {
            ForEach(DayListLayout.sections(blocks: blocks, columns: columns, time: timetable.timeText)) { section in
                Section(section.title) {
                    ForEach(section.blocks) { block in
                        let classification = timetable.classification(for: block)
                        let project = timetable.projectName(block.projectId)
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            HStack(alignment: .firstTextBaseline, spacing: Spacing.s) {
                                Text(text.listRow(block, project: project))
                                    .font(.rmt.body)
                                    .strikethrough(classification.treatment == .skipped)
                                Spacer(minLength: 0)
                                if let glyph = Self.glyph(classification) {
                                    Image(systemName: glyph).foregroundStyle(model.palette.slot(for: block.projectId).ink)
                                }
                            }
                            if staged[block.id] != nil {
                                Label("Change waiting for you to confirm", systemImage: "sparkles")
                                    .font(.rmt.callout)
                                    .foregroundStyle(ColorToken.proposal)
                            }
                        }
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel(text.accessibilityLabel(block, project: project, classification: classification))
                        .modifier(BlockActionsModifier(block: block, classification: classification, onEditTime: onEditTime))
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    /// Second carrier next to the text (principle 4).
    static func glyph(_ classification: BlockClassification) -> String? {
        switch classification.treatment {
        case .completed: "checkmark.circle.fill"
        case .skipped: "arrow.uturn.forward.circle"
        default: classification.isPinned ? "pin.fill" : nil
        }
    }
}
