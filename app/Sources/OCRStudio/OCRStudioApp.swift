import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    /// Files dropped on the Dock icon or opened with "Open With".
    func application(_ application: NSApplication, open urls: [URL]) {
        Task { @MainActor in JobStore.shared.add(urls) }
    }
}

@main
struct OCRStudioApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var store = JobStore.shared

    var body: some Scene {
        WindowGroup("OCR Studio") {
            ContentView()
                .environmentObject(store)
                .frame(minWidth: 940, minHeight: 600)
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Add Files…") { store.pickFiles() }.keyboardShortcut("o")
            }
            CommandGroup(after: .toolbar) {
                Button("Clear Finished") { store.clearFinished() }.keyboardShortcut("k", modifiers: [.command, .shift])
            }
        }

        Settings {
            SettingsView().environmentObject(store)
        }
    }
}
