import CoreGraphics

struct EdgeGeometry {
    static let headerHeight: CGFloat = 28
    static let contentSpacing: CGFloat = 8
    static let activityRowHeight: CGFloat = 33
    static let activityRowSpacing: CGFloat = 4
    static let visibleActivityRows = 2
    static let activityHeight = activityRowHeight * CGFloat(visibleActivityRows)
        + activityRowSpacing * CGFloat(visibleActivityRows - 1)
    static let bottomInset: CGFloat = 10

    let panel: CGRect
    let trigger: CGRect
    let contentTopInset: CGFloat
    let menuBarVisible: Bool

    init(screen: CGRect, visible: CGRect, safeTop: CGFloat) {
        contentTopInset = max(13, safeTop + 4)
        let width = min(392, max(1, screen.width - 20))
        let height = contentTopInset + Self.headerHeight + Self.contentSpacing + Self.activityHeight + Self.bottomInset
        panel = CGRect(x: screen.midX - width / 2, y: screen.maxY - height, width: width, height: height)
        let triggerWidth = min(98, width)
        trigger = CGRect(x: screen.midX - triggerWidth / 2, y: screen.maxY - 9, width: triggerWidth, height: 9)
        menuBarVisible = screen.maxY - visible.maxY >= 10
    }

    func hitsTrigger(_ point: CGPoint) -> Bool {
        point.x >= trigger.minX && point.x < trigger.maxX &&
            point.y > trigger.minY && point.y <= trigger.maxY
    }
}
