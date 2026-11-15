import Foundation

/// Which design/notifications.md §1 row a block-start notification uses.
enum BlockStartVariant: Hashable, Sendable {
    case lastOfWeek
    case deadlineSoon(days: Int)
    case pinned
    case perennialAsUsual
    case standard
}

/// Copy for design/notifications.md §1, §2 and §4. Display text only.
enum NotificationCopy {
    /// "1 hour", "2 hours", "90 minutes", "2 hours 30 minutes": minutes under 2 h when not whole
    /// hours, whole hours spelled out, otherwise hours and minutes.
    static func duration(minutes: Int) -> String {
        let h = minutes / 60, m = minutes % 60
        let hours = h == 1 ? "1 hour" : "\(h) hours"
        if m == 0, h > 0 { return hours }
        if minutes < 120 { return minutes == 1 ? "1 minute" : "\(minutes) minutes" }
        return "\(hours) \(m == 1 ? "1 minute" : "\(m) minutes")"
    }

    static func blockStart(project: String, minutes: Int, variant: BlockStartVariant) -> (title: String, body: String) {
        let d = duration(minutes: minutes)
        let body: String
        switch variant {
        case .lastOfWeek:
            body = "Last \(project) session this week: \(d)."
        case .deadlineSoon(let days):
            body = "\(d) on \(project). \(daysToGo(days))"
        case .pinned:
            body = "Your \(d) for \(project), as you set it."
        case .perennialAsUsual:
            body = "\(d) of \(project), same as always."
        case .standard:
            body = "Next \(d): \(project). That's all the time it gets today."
        }
        return (project, body)
    }

    /// "{days} days to go." with singular and due-today forms (the table only shows the plural).
    static func daysToGo(_ days: Int) -> String {
        switch days {
        case 0: "Due today."
        case 1: "1 day to go."
        default: "\(days) days to go."
        }
    }

    static func blockSoon(project: String, minutes: Int, startText: String) -> (title: String, body: String) {
        ("In 10 minutes: \(project)", "\(duration(minutes: minutes)) starting at \(startText). Wrap up what you're doing.")
    }

    static func drift(project: String, startText: String) -> (title: String, body: String) {
        ("A meeting landed on your plan", "\(project) at \(startText) clashes with something new in your calendar. Tap to see a fix.")
    }
}

/// What the scheduler plans from: the live plan plus the reference data the copy needs.
struct NotificationSnapshot: Sendable {
    var plan: WeekPlan?
    var pins: [BlockPin] = []
    var projects: [UUID: Project] = [:]
    /// Display names for every project in the plan (includes names the timetable resolves from
    /// the directory).
    var projectNames: [UUID: String] = [:]

    static let empty = NotificationSnapshot()
}

/// The rolling 48-hour window (design/notifications.md): pure, deterministic, unit-tested.
/// Picks which planned blocks get a notification and words each one; never moves a block.
struct BlockNotificationPlanner {
    static let windowSeconds: TimeInterval = 48 * 3600
    /// iOS keeps at most 64 pending local notifications per app.
    static let pendingCap = 64
    static let warningLeadSeconds: TimeInterval = 10 * 60
    static let startPrefix = "block-start."
    static let soonPrefix = "block-soon."

    var now: Date
    var warningsEnabled: Bool
    /// Device calendar: display times ("starting at 9:00 AM") and "same time as usual".
    var calendar: Calendar
    var locale: Locale = .autoupdatingCurrent

    static func startIdentifier(lineage: UUID) -> String { startPrefix + lineage.pathComponent }
    static func soonIdentifier(lineage: UUID) -> String { soonPrefix + lineage.pathComponent }
    static func isBlockNotification(_ identifier: String) -> Bool {
        identifier.hasPrefix(startPrefix) || identifier.hasPrefix(soonPrefix)
    }

    /// Block starts first (by time), then 10-minute warnings in the capacity left under 64.
    func requests(for snapshot: NotificationSnapshot) -> [LocalNotificationRequest] {
        guard let plan = snapshot.plan else { return [] }
        let horizon = now.addingTimeInterval(Self.windowSeconds)
        let upcoming = plan.blocks
            .filter { $0.state == .planned && $0.span.start > now && $0.span.start <= horizon }
            .sorted { ($0.span.start, $0.lineageId.uuidString) < ($1.span.start, $1.lineageId.uuidString) }
        let pinned = PinJoin.pinnedBlockIds(blocks: plan.blocks, pins: snapshot.pins)
        let starts = upcoming.prefix(Self.pendingCap).map { startRequest($0, plan: plan, snapshot: snapshot, pinned: pinned) }
        guard warningsEnabled else { return starts }
        let room = Self.pendingCap - starts.count
        let warnings = upcoming
            .filter { $0.span.start.addingTimeInterval(-Self.warningLeadSeconds) > now }
            .prefix(max(0, room))
            .map { soonRequest($0, snapshot: snapshot) }
        return starts + warnings
    }

    func variant(for block: Block, plan: WeekPlan, project: Project?, pinned: Set<UUID>) -> BlockStartVariant {
        let sameProject = plan.blocks.filter { $0.projectId == block.projectId && $0.id != block.id && $0.state != .skipped }
        if !sameProject.contains(where: { $0.span.start > block.span.start }) {
            return .lastOfWeek
        }
        if case .deadline(let target, _)? = project?.goal, let days = daysToTarget(block, target: target, plan: plan), (0...3).contains(days) {
            return .deadlineSoon(days: days)
        }
        if pinned.contains(block.id) {
            return .pinned
        }
        if case .perennial? = project?.goal {
            let clock = clockTime(block.span.start)
            let usual = sameProject.contains { other in
                !calendar.isDate(other.span.start, inSameDayAs: block.span.start) && clockTime(other.span.start) == clock
            }
            if usual { return .perennialAsUsual }
        }
        return .standard
    }

    // MARK: private

    private func startRequest(_ block: Block, plan: WeekPlan, snapshot: NotificationSnapshot, pinned: Set<UUID>) -> LocalNotificationRequest {
        let name = snapshot.projectNames[block.projectId] ?? snapshot.projects[block.projectId]?.name ?? "Your next block"
        let variant = variant(for: block, plan: plan, project: snapshot.projects[block.projectId], pinned: pinned)
        let copy = NotificationCopy.blockStart(project: name, minutes: TimeText.minutes(block.span), variant: variant)
        return LocalNotificationRequest(
            identifier: Self.startIdentifier(lineage: block.lineageId),
            title: copy.title,
            body: copy.body,
            categoryIdentifier: NotificationCategoryID.blockStart,
            fireDate: block.span.start,
            userInfo: userInfo(block, kind: .blockStart)
        )
    }

    private func soonRequest(_ block: Block, snapshot: NotificationSnapshot) -> LocalNotificationRequest {
        let name = snapshot.projectNames[block.projectId] ?? snapshot.projects[block.projectId]?.name ?? "Your next block"
        let copy = NotificationCopy.blockSoon(
            project: name,
            minutes: TimeText.minutes(block.span),
            startText: TimeText(calendar: calendar, locale: locale).time(block.span.start)
        )
        return LocalNotificationRequest(
            identifier: Self.soonIdentifier(lineage: block.lineageId),
            title: copy.title,
            body: copy.body,
            categoryIdentifier: NotificationCategoryID.blockSoon,
            fireDate: block.span.start.addingTimeInterval(-Self.warningLeadSeconds),
            userInfo: userInfo(block, kind: .blockSoon)
        )
    }

    private func userInfo(_ block: Block, kind: NotificationRoute.Kind) -> [String: String] {
        [
            NotificationRoute.Key.kind: kind.rawValue,
            NotificationRoute.Key.blockId: block.id.pathComponent,
            NotificationRoute.Key.lineageId: block.lineageId.pathComponent,
        ]
    }

    /// Whole local days from the block's date to the target date, both read in the plan's zone
    /// (target dates are local to `preferences.timezone`, D10). Copy only.
    private func daysToTarget(_ block: Block, target: LocalDate, plan: WeekPlan) -> Int? {
        guard let zone = TimeZone(identifier: plan.timezone),
              let blockDay = LocalDate.from(block.span.start, timeZone: zone),
              let from = blockDay.middayInstant(timeZone: zone),
              let to = target.middayInstant(timeZone: zone)
        else { return nil }
        let calendar = LocalDate.gregorian(timeZone: zone)
        return calendar.dateComponents([.day], from: calendar.startOfDay(for: from), to: calendar.startOfDay(for: to)).day
    }

    private func clockTime(_ date: Date) -> [Int] {
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        return [parts.hour ?? 0, parts.minute ?? 0]
    }
}

/// What a notification response asks the app to do.
enum NotificationRoute: Hashable, Sendable {
    /// Open → Day view at the block (tap, or the Open action).
    case openBlock(lineageId: UUID, blockId: UUID?)
    /// Skip this → `PATCH /blocks/{id}` `skip`.
    case skipBlock(lineageId: UUID, blockId: UUID?)
    /// Drift alert → proposal review.
    case drift(changesetId: UUID?)

    enum Kind: String, Sendable {
        case blockStart = "block_start"
        case blockSoon = "block_soon"
        case drift
    }

    enum Key {
        static let kind = "kind"
        static let blockId = "block_id"
        static let lineageId = "lineage_id"
        static let changesetId = "changeset_id"
    }

    /// `actionIdentifier` is the UN action (default tap, dismiss, or one of `NotificationActionID`).
    init?(actionIdentifier: String, userInfo: [String: String], defaultAction: String, dismissAction: String) {
        guard actionIdentifier != dismissAction, let kind = userInfo[Key.kind].flatMap(Kind.init(rawValue:)) else { return nil }
        if kind == .drift {
            self = .drift(changesetId: userInfo[Key.changesetId].flatMap(UUID.init(uuidString:)))
            return
        }
        guard let lineage = userInfo[Key.lineageId].flatMap(UUID.init(uuidString:)) else { return nil }
        let blockId = userInfo[Key.blockId].flatMap(UUID.init(uuidString:))
        switch actionIdentifier {
        case NotificationActionID.skip where kind == .blockStart:
            self = .skipBlock(lineageId: lineage, blockId: blockId)
        case NotificationActionID.open, defaultAction:
            self = .openBlock(lineageId: lineage, blockId: blockId)
        default:
            return nil
        }
    }
}
