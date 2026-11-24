import XCTest
@testable import ReclaimMyTime

final class PaletteAssignmentTests: XCTestCase {
    func testLowestFreeSlot() {
        XCTAssertEqual(PaletteAssignment.nextSlot(activeAssignments: [:]), 0)
        XCTAssertEqual(PaletteAssignment.nextSlot(activeAssignments: [UUID(): 0, UUID(): 1, UUID(): 3]), 2)
    }

    func testReuseAfterTenPicksLeastUsedLowest() {
        var assignments: [UUID: Int] = [:]
        for slot in 0..<10 { assignments[UUID()] = slot }
        XCTAssertEqual(PaletteAssignment.nextSlot(activeAssignments: assignments), 0)
        assignments[UUID()] = 0
        XCTAssertEqual(PaletteAssignment.nextSlot(activeAssignments: assignments), 1)
    }

    func testSlotIndexWraps() {
        XCTAssertEqual(PaletteSlot(13).index, 3)
        XCTAssertEqual(PaletteSlot(-1).index, 9)
        XCTAssertEqual(PaletteSlot.names.count, PaletteSlot.count)
    }

    @MainActor
    func testStoreIsStablePersistentAndReleases() throws {
        let suite = "PaletteTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let a = UUID(), b = UUID(), c = UUID()

        let store = ProjectPaletteStore(defaults: defaults)
        XCTAssertEqual(store.slot(for: a).index, 0)
        XCTAssertEqual(store.slot(for: b).index, 1)
        XCTAssertEqual(store.slot(for: a).index, 0, "stable")

        let reloaded = ProjectPaletteStore(defaults: defaults)
        XCTAssertEqual(reloaded.slot(for: b).index, 1, "persisted")
        reloaded.release(a)
        XCTAssertEqual(reloaded.slot(for: c).index, 0, "released slot is reused as the lowest free")
    }
}

final class DesignTokenTests: XCTestCase {
    func testSpacingRadiusAndHitTargetValues() {
        XCTAssertEqual([Spacing.xs, Spacing.s, Spacing.m, Spacing.l, Spacing.xl, Spacing.xxl], [4, 8, 12, 16, 24, 40])
        XCTAssertEqual(Radius.block, 6)
        XCTAssertEqual(Radius.card, 14)
        XCTAssertEqual(HitTarget.min, 44)
    }

    func testEveryColorAssetResolvesInBothAppearances() {
        let names = ["bg", "surface", "surfaceRaised", "textPrimary", "textSecondary", "gridLine", "gridLineHalf",
                     "nowLine", "busy", "accent", "onAccent", "proposal", "warning", "positive", "destructive"]
            + (0..<10).flatMap { ["projectFill\($0)", "projectInk\($0)"] }
        let bundle = Bundle(for: AppModel.self)
        for name in names {
            for style in [UIUserInterfaceStyle.light, .dark] {
                XCTAssertNotNil(UIColor(named: name, in: bundle, compatibleWith: UITraitCollection(userInterfaceStyle: style)), "\(name) \(style.rawValue)")
            }
        }
        // Spot-check light/dark pairs against tokens.md.
        let traitsLight = UITraitCollection(userInterfaceStyle: .light)
        let traitsDark = UITraitCollection(userInterfaceStyle: .dark)
        let accent = UIColor(named: "accent", in: bundle, compatibleWith: nil)
        XCTAssertEqual(accent.map { hex($0.resolvedColor(with: traitsLight)) }, "2C5F2E")
        XCTAssertEqual(accent.map { hex($0.resolvedColor(with: traitsDark)) }, "8BC98F")
        let bg = UIColor(named: "bg", in: bundle, compatibleWith: nil)
        XCTAssertEqual(bg.map { hex($0.resolvedColor(with: traitsLight)) }, "F0EAD6")
        XCTAssertEqual(bg.map { hex($0.resolvedColor(with: traitsDark)) }, "121212")
    }

    private func hex(_ color: UIColor) -> String {
        var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        color.getRed(&r, green: &g, blue: &b, alpha: &a)
        return String(format: "%02X%02X%02X", Int((r * 255).rounded()), Int((g * 255).rounded()), Int((b * 255).rounded()))
    }
}
