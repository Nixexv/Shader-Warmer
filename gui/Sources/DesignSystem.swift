import SwiftUI

// Native SwiftUI tokens, inspired by the supplied launcher reference.
enum WarmerTheme {
    static let background = Color(hex: 0x131414)
    static let rail = Color(hex: 0x171818)
    static let surface = Color(hex: 0x1D1E1F)
    static let raised = Color(hex: 0x252627)
    static let border = Color(hex: 0x343638)
    static let text = Color(hex: 0xF1F1EF)
    static let secondary = Color(hex: 0xABADAF)
    static let accent = Color(hex: 0xE9A66F)
    static let ink = Color(hex: 0x211810)
    static let success = Color(hex: 0x86BD98)
    static let error = Color(hex: 0xE7A194)
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB, red: Double((hex >> 16) & 255) / 255,
                  green: Double((hex >> 8) & 255) / 255, blue: Double(hex & 255) / 255, opacity: 1)
    }
}

struct WarmerButtonStyle: ButtonStyle {
    var prominent = false
    var compact = false
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: compact ? 12 : 13, weight: .semibold))
            .lineLimit(1)
            .padding(.horizontal, compact ? 13 : 18)
            .frame(minHeight: compact ? 34 : 44)
            .foregroundStyle(prominent ? WarmerTheme.ink : WarmerTheme.text)
            .background(prominent ? WarmerTheme.accent : WarmerTheme.raised,
                        in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(prominent ? Color.clear : WarmerTheme.border))
            .opacity(enabled ? (configuration.isPressed ? 0.78 : 1) : 0.38)
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
    }
}

struct WarmerPanel<Content: View>: View {
    var padding: CGFloat = 24
    @ViewBuilder var content: () -> Content
    var body: some View {
        content().padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(WarmerTheme.surface, in: RoundedRectangle(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).stroke(WarmerTheme.border.opacity(0.75)))
    }
}

struct StatusBadge: View {
    let text: String
    var color = WarmerTheme.accent
    var icon = "circle.fill"
    var body: some View {
        Label(text, systemImage: icon)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(color)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(color.opacity(0.1), in: Capsule())
    }
}

struct SectionTitle: View {
    let title: String
    var subtitle: String? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.system(size: 16, weight: .semibold)).foregroundStyle(WarmerTheme.text)
            if let subtitle { Text(subtitle).font(.system(size: 12)).foregroundStyle(WarmerTheme.secondary) }
        }
    }
}

struct EmptyLibraryState: View {
    let title: String
    let message: String
    var symbol = "square.grid.2x2"
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: symbol).font(.system(size: 32, weight: .light)).foregroundStyle(WarmerTheme.accent)
            Text(title).font(.system(size: 19, weight: .semibold))
            Text(message).font(.system(size: 13)).foregroundStyle(WarmerTheme.secondary)
                .multilineTextAlignment(.center).frame(maxWidth: 360)
        }.frame(maxWidth: .infinity).padding(.vertical, 80)
    }
}
