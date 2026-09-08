import Cocoa

let PROXY = ProcessInfo.processInfo.environment["AGENTROUTER_PROXY"] ?? "agentrouter-proxy"
let INTERVAL = Double(ProcessInfo.processInfo.environment["AGENTROUTER_TRAY_INTERVAL"] ?? "") ?? 120.0

var proxyPath: String = PROXY

func runProxy(_ args: [String]) -> (out: String, ok: Bool) {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/bin/zsh")
    let cmd = args.isEmpty ? "\(proxyPath)" : "\(proxyPath) \(args.joined(separator: " "))"
    proc.arguments = ["-c", cmd]
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError = pipe
    proc.standardInput = FileHandle.nullDevice
    try? proc.run()
    proc.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return (String(data: data, encoding: .utf8) ?? "", proc.terminationStatus == 0)
}

struct QuotaInfo {
    var remaining: Double = -1
    var pct: Double = 0
    var dollars: Double = 0
    var usedDollars: Double = 0
    var requests: Int = 0
    var valid: Bool = false
    var timestamp: String = ""
}

func fetchQuota() -> QuotaInfo {
    var info = QuotaInfo()
    let (out, ok) = runProxy(["quota", "--raw"])
    guard ok else { return info }
    // Extract JSON from output (skip any non-JSON lines from shell startup)
    let lines = out.components(separatedBy: "\n")
    var jsonStr = ""
    var depth = 0
    for line in lines {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("{") && depth == 0 {
            depth = trimmed.filter { $0 == "{" }.count - trimmed.filter { $0 == "}" }.count
            jsonStr = trimmed
        } else if depth > 0 {
            jsonStr += trimmed
            depth += trimmed.filter { $0 == "{" }.count - trimmed.filter { $0 == "}" }.count
        }
    }
    guard !jsonStr.isEmpty,
          let data = jsonStr.data(using: .utf8),
          let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          root["error"] == nil else {
        return info
    }
    let quota = (root["quota"] as? NSNumber)?.doubleValue ?? 0
    let used = (root["used_quota"] as? NSNumber)?.doubleValue ?? 0
    let total = quota + used
    guard total > 0 else { return info }
    info.remaining = quota
    info.pct = quota / total * 100
    info.dollars = quota / 500_000
    info.usedDollars = used / 500_000
    info.requests = (root["request_count"] as? NSNumber)?.intValue ?? 0
    info.valid = true
    let now = Date()
    let f = DateFormatter()
    f.dateFormat = "HH:mm"
    info.timestamp = f.string(from: now)
    return info
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var menu = NSMenu()
    var timer: Timer?
    var currentInfo = QuotaInfo()

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "AR ..."

        menu.addItem(NSMenuItem(title: "AgentRouter Proxy", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Remaining: loading…", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Used: —", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Requests: —", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Updated: —", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())

        let refresh = NSMenuItem(title: "Refresh now", action: #selector(refreshNow), keyEquivalent: "r")
        refresh.target = self
        menu.addItem(refresh)

        let log = NSMenuItem(title: "Open log", action: #selector(openLog), keyEquivalent: "l")
        log.target = self
        menu.addItem(log)

        let dash = NSMenuItem(title: "Open dashboard", action: #selector(openDash), keyEquivalent: "d")
        dash.target = self
        menu.addItem(dash)

        menu.addItem(NSMenuItem.separator())

        let stop = NSMenuItem(title: "Stop proxy & quit", action: #selector(stopAndQuit), keyEquivalent: "q")
        stop.target = self
        menu.addItem(stop)

        statusItem.menu = menu
        refreshNow(nil)
        timer = Timer.scheduledTimer(withTimeInterval: INTERVAL, repeats: true) { [weak self] _ in
            self?.refreshNow(nil)
        }
    }

    @objc func refreshNow(_ sender: Any?) {
        currentInfo = fetchQuota()
        if currentInfo.valid {
            let pctStr = String(format: "%.1f%%", currentInfo.pct)
            statusItem.button?.title = "AR \(pctStr)"
            if let items = menu.items as? [NSMenuItem] {
                items[2].title = "Remaining: \(pctStr) ($\(String(format: "%.2f", currentInfo.dollars)))"
                items[3].title = "Used: $\(String(format: "%.2f", currentInfo.usedDollars))"
                items[4].title = "Requests: \(currentInfo.requests)"
                items[5].title = "Updated: \(currentInfo.timestamp)"
            }
        } else {
            statusItem.button?.title = "AR \u{26A0}\u{FE0F}"
            if let items = menu.items as? [NSMenuItem] {
                items[2].title = "Remaining: unavailable (cookie expired?)"
                items[3].title = "Used: —"
                items[4].title = "Requests: —"
                items[5].title = "Updated: —"
            }
        }
    }

    @objc func openLog(_ sender: Any?) {
        NSWorkspace.shared.open(URL(fileURLWithPath: "/tmp/agentrouter-opencode-proxy.log"))
    }

    @objc func openDash(_ sender: Any?) {
        NSWorkspace.shared.open(URL(string: "https://agentrouter.org/console")!)
    }

    @objc func stopAndQuit(_ sender: Any?) {
        let _ = runProxy(["stop"])
        NSApplication.shared.terminate(nil)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
