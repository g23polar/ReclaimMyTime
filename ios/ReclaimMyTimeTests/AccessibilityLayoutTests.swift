import SwiftUI
import UIKit
import XCTest
@testable import ReclaimMyTime

/// Dynamic Type through AX5 (tokens.md): cards reflow instead of truncating or overflowing.
@MainActor
final class AccessibilityLayoutTests: XCTestCase {
    private let width: CGFloat = 393

    private func fittedSize<V: View>(_ view: V, size: DynamicTypeSize) -> CGSize {
        let host = UIHostingController(rootView: view.environment(\.dynamicTypeSize, size).frame(width: width))
        return host.sizeThatFits(in: CGSize(width: width, height: .greatestFiniteMagnitude))
    }

    func testProjectCardReflowsAtAX5() {
        let card = ProjectCard(id: UUID(), draft: T52Fixtures.portfolioDraft)
        let view = ProjectSummaryCardView(card: card, slot: PaletteSlot(0), onEdit: {}, onConfirm: {})
        let regular = fittedSize(view, size: .large)
        let ax5 = fittedSize(view, size: .accessibility5)
        XCTAssertLessThanOrEqual(ax5.width, width, "no horizontal overflow at AX5")
        XCTAssertGreaterThan(ax5.height, regular.height * 2, "text scales and wraps at AX5")
    }

    func testPreferenceCardsStackAtAccessibilitySizes() {
        XCTAssertFalse(DayDiversityPicker.stacks(at: .large))
        XCTAssertFalse(DayDiversityPicker.stacks(at: .xxxLarge))
        XCTAssertTrue(DayDiversityPicker.stacks(at: .accessibility1))
        XCTAssertTrue(DayDiversityPicker.stacks(at: .accessibility5))
        let picker = DayDiversityPicker(selection: .constant(.mixed))
        let ax5 = fittedSize(picker, size: .accessibility5)
        XCTAssertLessThanOrEqual(ax5.width, width)
        XCTAssertGreaterThan(ax5.height, fittedSize(picker, size: .large).height)
    }

    func testBubblesFitAtAX5() {
        let ax5 = fittedSize(VStack { AgentBubble(text: ChatModel.openingMessage, state: .done); UserBubble(text: "Reading, two hours a week") }, size: .accessibility5)
        XCTAssertLessThanOrEqual(ax5.width, width)
    }
}
