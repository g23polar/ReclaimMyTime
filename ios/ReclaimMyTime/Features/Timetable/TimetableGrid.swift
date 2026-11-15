import SwiftUI

enum GridLayoutConstants {
    static let gutterWidth: CGFloat = 48
    static let headerHeight: CGFloat = 52
}

/// Hour lines, half-hour lines, column separators and the outside-window shade (timetable.md §1).
/// Decorative: hidden from VoiceOver.
struct GridBackground: View {
    let columnCount: Int
    let metrics: GridMetrics
    let columnWidth: CGFloat
    /// Per column: wall-clock minute ranges outside the schedulable windows.
    let outsideBands: [[(Double, Double)]]

    var body: some View {
        Canvas { context, size in
            context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(ColorToken.surface))
            for (column, bands) in outsideBands.enumerated() {
                for (start, end) in bands {
                    let rect = CGRect(
                        x: CGFloat(column) * columnWidth,
                        y: metrics.y(minuteOfDay: start),
                        width: columnWidth,
                        height: metrics.y(minuteOfDay: end) - metrics.y(minuteOfDay: start)
                    )
                    context.fill(Path(rect), with: .color(ColorToken.bg))
                    context.fill(Path(rect), with: .color(ColorToken.gridLineHalf.opacity(0.5)))
                }
            }
            for hour in metrics.startHour...metrics.endHour {
                let y = metrics.y(minuteOfDay: Double(hour * 60))
                var line = Path()
                line.move(to: CGPoint(x: 0, y: y))
                line.addLine(to: CGPoint(x: size.width, y: y))
                context.stroke(line, with: .color(ColorToken.gridLine), lineWidth: 1)
                if hour < metrics.endHour {
                    var half = Path()
                    let halfY = y + metrics.pointsPerHour / 2
                    half.move(to: CGPoint(x: 0, y: halfY))
                    half.addLine(to: CGPoint(x: size.width, y: halfY))
                    context.stroke(half, with: .color(ColorToken.gridLineHalf), lineWidth: 1)
                }
            }
            for column in 0...columnCount {
                var line = Path()
                let x = CGFloat(column) * columnWidth
                line.move(to: CGPoint(x: x, y: 0))
                line.addLine(to: CGPoint(x: x, y: size.height))
                context.stroke(line, with: .color(ColorToken.gridLine), lineWidth: 0.5)
            }
        }
        .frame(width: CGFloat(columnCount) * columnWidth, height: metrics.height)
        .accessibilityHidden(true)
    }
}

/// Hour labels on the leading gutter, plus scroll anchors (`hour-N`).
struct HourGutter: View {
    let metrics: GridMetrics
    var time = TimeText()

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.clear.frame(width: GridLayoutConstants.gutterWidth, height: metrics.height)
            ForEach(metrics.hours, id: \.self) { hour in
                Text(label(hour))
                    .font(.rmt.gridLabel)
                    .foregroundStyle(ColorToken.textSecondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                    .frame(width: GridLayoutConstants.gutterWidth - 6, alignment: .trailing)
                    .offset(y: metrics.y(minuteOfDay: Double(hour * 60)) - 6)
                    .opacity(hour == metrics.endHour ? 0 : 1)
            }
        }
        .background(alignment: .top) {
            // Scroll anchors need real layout positions (`.offset` doesn't move layout).
            VStack(spacing: 0) {
                ForEach(metrics.hours.dropLast(), id: \.self) { hour in
                    Color.clear.frame(height: metrics.pointsPerHour).id("hour-\(hour)")
                }
            }
        }
        .accessibilityHidden(true)
    }

    private func label(_ hour: Int) -> String {
        var parts = DateComponents()
        parts.hour = hour % 24
        parts.minute = 0
        let date = time.calendar.date(from: parts) ?? Date()
        return date.formatted(Date.FormatStyle(date: .omitted, time: .omitted, locale: time.locale, calendar: time.calendar, timeZone: time.calendar.timeZone).hour())
    }
}

/// The now marker (timetable.md §3): 1.5 pt line across today's column, 6 pt dot at its leading
/// edge, time label on the gutter.
struct NowLine: View {
    let y: CGFloat
    let columnX: CGFloat
    let columnWidth: CGFloat
    let label: String

    var body: some View {
        ZStack(alignment: .topLeading) {
            Text(label)
                .font(.rmt.gridLabel)
                .foregroundStyle(ColorToken.nowLine)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
                .padding(.horizontal, 2)
                .background(ColorToken.bg)
                .frame(width: GridLayoutConstants.gutterWidth - 2, alignment: .trailing)
                .offset(y: y - 7)
            Rectangle()
                .fill(ColorToken.nowLine)
                .frame(width: columnWidth, height: 1.5)
                .offset(x: GridLayoutConstants.gutterWidth + columnX, y: y - 0.75)
            Circle()
                .fill(ColorToken.nowLine)
                .frame(width: 6, height: 6)
                .offset(x: GridLayoutConstants.gutterWidth + columnX - 3, y: y - 3)
        }
        .allowsHitTesting(false)
        .accessibilityElement()
        .accessibilityLabel("Now, \(label)")
    }
}

/// Weekday + date headers; today's uses `accent` text with a filled circle behind the date.
struct DayHeaderRow: View {
    let columns: [DayColumn]
    let columnWidth: CGFloat
    let today: Date
    var time = TimeText()
    var onTap: ((DayColumn) -> Void)?

    var body: some View {
        HStack(spacing: 0) {
            Color.clear.frame(width: GridLayoutConstants.gutterWidth)
            ForEach(columns) { column in
                let isToday = column.contains(today)
                Button {
                    onTap?(column)
                } label: {
                    VStack(spacing: 2) {
                        Text(time.weekdayShort(column.start))
                            .font(.rmt.gridLabel)
                            .foregroundStyle(isToday ? ColorToken.accent : ColorToken.textSecondary)
                        Text(time.dayNumber(column.start))
                            .font(.rmt.gridLabel)
                            .foregroundStyle(isToday ? ColorToken.onAccent : ColorToken.textPrimary)
                            .frame(minWidth: 22, minHeight: 22)
                            .background {
                                if isToday { Circle().fill(ColorToken.accent) }
                            }
                    }
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                    .frame(width: columnWidth, height: GridLayoutConstants.headerHeight)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(onTap == nil)
                .accessibilityLabel(time.dayTitle(column.start) + (isToday ? ", today" : ""))
                .accessibilityHint(onTap == nil ? "" : "Opens this day")
            }
        }
        .frame(height: GridLayoutConstants.headerHeight)
        .background(ColorToken.bg)
    }
}

/// Places a view at a rect inside a top-leading ZStack.
extension View {
    func placed(in rect: CGRect) -> some View {
        frame(width: rect.width, height: rect.height).offset(x: rect.minX, y: rect.minY)
    }
}
