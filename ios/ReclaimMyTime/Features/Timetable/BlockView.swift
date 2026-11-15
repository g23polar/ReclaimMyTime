import SwiftUI

/// Everything a block card needs to draw itself.
struct BlockCardModel: Hashable {
    let project: String
    let slot: PaletteSlot
    let span: Span
    var treatment: BlockTreatment = .planned
    var isPinned = false
    var gcalStatus: GcalSync = .synced
}

/// How a card is decorated: the live layers (timetable.md §3) or a proposal/diff state
/// (diff-and-infeasibility.md §2). Each state has at least two carriers (principle 4).
enum BlockCardStyle: Hashable {
    case live
    /// Proposal outline; `badge` adds `sparkles`, `newLabel` the "New" micro-label (both off in
    /// first-week mode, where everything is new).
    case proposal(badge: Bool, newLabel: Bool)
    /// Unchanged in a diff: normal block at 60%.
    case unchanged
    /// Dashed ghost in project ink; `struck` strikes the title (removed); `opacity` 0.4 for a
    /// moved block's old spot.
    case ghost(struck: Bool, opacity: Double)
}

/// Block anatomy (timetable.md §4): 3 pt leading color bar, project name, optional time/duration
/// line, pin and sync glyphs.
struct BlockView: View {
    let card: BlockCardModel
    var style: BlockCardStyle = .live
    var content: BlockContent = .nameAndMeta
    /// Fraction of an in-progress block already elapsed (darkened 10%).
    var elapsedFraction: CGFloat = 0
    /// Extra line under the name ("Now 2 h" for a resize).
    var caption: String?
    /// Week-view columns: name may wrap/shrink, glyphs move under it.
    var compact = false
    var time = TimeText()
    var onSyncTap: (() -> Void)?

    var body: some View {
        let ink = card.slot.ink
        ZStack(alignment: .topLeading) {
            background
            HStack(spacing: 0) {
                if showsEdgeBar {
                    Rectangle().fill(card.slot.edge).frame(width: 3)
                }
                VStack(alignment: .leading, spacing: 1) {
                    if compact {
                        // Narrow week columns: the name gets the full width; glyphs sit below.
                        Text(card.project)
                            .font(.rmt.blockTitle)
                            .strikethrough(isStruck, color: ink)
                            .lineLimit(2)
                            .minimumScaleFactor(0.7)
                        trailingGlyph
                    } else {
                        HStack(alignment: .firstTextBaseline, spacing: 2) {
                            Text(card.project)
                                .font(.rmt.blockTitle)
                                .strikethrough(isStruck, color: ink)
                                .lineLimit(1)
                            Spacer(minLength: 0)
                            trailingGlyph
                        }
                    }
                    if content == .nameAndMeta {
                        HStack(spacing: 2) {
                            Text(BlockText(time: time).meta(card.span))
                                .font(.rmt.blockMeta)
                                .lineLimit(1)
                            Spacer(minLength: 0)
                            syncGlyph
                        }
                    }
                    if let caption {
                        Text(caption)
                            .font(.rmt.blockMeta)
                            .foregroundStyle(ColorToken.proposal)
                            .lineLimit(1)
                    }
                    if case .proposal(_, true) = style {
                        Text("New")
                            .font(.rmt.blockMeta.weight(.semibold))
                            .foregroundStyle(ColorToken.proposal)
                    }
                }
                .foregroundStyle(ink)
                .padding(.horizontal, Spacing.xs)
                .padding(.vertical, 2)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: Radius.block))
        .overlay { outline }
        .opacity(opacity)
    }

    private var showsEdgeBar: Bool {
        switch style {
        case .ghost: false
        default: card.treatment != .skipped
        }
    }

    private var isStruck: Bool {
        switch style {
        case .ghost(let struck, _): struck
        default: card.treatment == .skipped
        }
    }

    private var opacity: Double {
        switch style {
        case .unchanged: 0.6
        case .ghost(_, let value): value
        case .live, .proposal: 1
        }
    }

    @ViewBuilder
    private var background: some View {
        switch style {
        case .ghost:
            Color.clear
        default:
            switch card.treatment {
            case .skipped:
                Color.clear
            case .completed:
                card.slot.fill.opacity(0.55)
            case .inProgress:
                card.slot.fill.overlay(alignment: .top) {
                    GeometryReader { proxy in
                        Color.black.opacity(0.1).frame(height: proxy.size.height * min(max(elapsedFraction, 0), 1))
                    }
                }
            case .planned, .pastPlanned:
                card.slot.fill
            }
        }
    }

    @ViewBuilder
    private var outline: some View {
        let shape = RoundedRectangle(cornerRadius: Radius.block)
        switch style {
        case .ghost:
            shape.strokeBorder(card.slot.ink, style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
        case .proposal:
            shape.strokeBorder(ColorToken.proposal, lineWidth: 1.5)
        case .live, .unchanged:
            if card.treatment == .inProgress {
                shape.strokeBorder(ColorToken.nowLine, lineWidth: 2)
            } else if card.treatment == .skipped {
                shape.strokeBorder(card.slot.ink.opacity(0.5), lineWidth: 1)
            } else if card.isPinned {
                shape.strokeBorder(card.slot.ink, lineWidth: 1.5)
            }
        }
    }

    @ViewBuilder
    private var trailingGlyph: some View {
        switch style {
        case .proposal(let badge, _):
            if badge {
                Image(systemName: "sparkles").font(.rmt.blockMeta).foregroundStyle(ColorToken.proposal)
            }
        case .ghost:
            EmptyView()
        case .live, .unchanged:
            if card.treatment == .completed {
                Image(systemName: "checkmark.circle.fill").font(.rmt.blockMeta).foregroundStyle(ColorToken.positive)
            } else if card.treatment == .skipped {
                Image(systemName: "arrow.uturn.forward.circle").font(.rmt.blockMeta)
            } else if card.isPinned {
                Image(systemName: "pin.fill").font(.rmt.blockMeta)
            }
        }
    }

    @ViewBuilder
    private var syncGlyph: some View {
        if case .live = style {
            switch card.gcalStatus {
            case .synced:
                EmptyView()
            case .pending:
                Image(systemName: "arrow.triangle.2.circlepath")
                    .font(.rmt.blockMeta)
                    .foregroundStyle(ColorToken.textSecondary)
            case .failed:
                Button {
                    onSyncTap?()
                } label: {
                    Image(systemName: "exclamationmark.arrow.triangle.2.circlepath")
                        .font(.rmt.blockMeta)
                        .foregroundStyle(ColorToken.warning)
                }
                .buttonStyle(.plain)
                .accessibilityHidden(true)
            }
        }
    }
}

/// Forbid span (timetable.md §3): no fill, 1 pt dashed `textSecondary` outline + `nosign`. A
/// staged delete draws the same shape in `proposal`.
struct ForbidView: View {
    var isProposal = false

    var body: some View {
        let color = isProposal ? ColorToken.proposal : ColorToken.textSecondary
        RoundedRectangle(cornerRadius: Radius.block)
            .strokeBorder(color, style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
            .overlay(alignment: .topTrailing) {
                HStack(spacing: 2) {
                    if isProposal { Image(systemName: "sparkles") }
                    Image(systemName: "nosign")
                }
                .font(.rmt.blockMeta)
                .foregroundStyle(color)
                .padding(3)
            }
    }
}

/// Calendar busy (timetable.md §3; design gap G1): `busy` fill + 45° hatch + `calendar` glyph when
/// ≥ 30 min. Only drawn if busy data exists.
struct BusyView: View {
    let showsGlyph: Bool

    var body: some View {
        ZStack(alignment: .topLeading) {
            ColorToken.busy
            Canvas { context, size in
                var path = Path()
                var x: CGFloat = -size.height
                while x < size.width {
                    path.move(to: CGPoint(x: x, y: size.height))
                    path.addLine(to: CGPoint(x: x + size.height, y: 0))
                    x += 6
                }
                context.stroke(path, with: .color(ColorToken.textSecondary.opacity(0.35)), lineWidth: 1)
            }
            if showsGlyph {
                Image(systemName: "calendar")
                    .font(.rmt.blockMeta)
                    .foregroundStyle(ColorToken.textSecondary)
                    .padding(3)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: Radius.block))
    }
}
