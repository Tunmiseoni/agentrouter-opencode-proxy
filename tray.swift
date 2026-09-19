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

struct PoolKeyInfo {
    var name: String = ""
    var remaining: Double? = nil
    var spent: Double? = nil
    var enabled: Bool = true
    var dead: Bool = false
    var unanchored: Bool = false
    var usageUnknown: Bool = false
    var recalibrated: String? = nil
}

struct PoolInfo {
    var routingEnabled: Bool = false
    var configured: Int = 0
    var totalRemaining: Double = 0
    var usageUnknownCount: Int = 0
    var keys: [PoolKeyInfo] = []
    var valid: Bool = false

    var hasKeys: Bool { !keys.isEmpty }
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

func jsonBool(_ v: Any?) -> Bool? {
    if let b = v as? Bool { return b }
    if let n = v as? NSNumber { return n.boolValue }
    return nil
}

func fetchPool() -> PoolInfo {
    var info = PoolInfo()
    let (out, ok) = runProxy(["pool-status", "--raw"])
    guard ok else { return info }
    // pool-status --raw emits a single JSON object; skip any shell warm-up lines.
    guard let start = out.firstIndex(of: "{"),
          let data = String(out[start...]).data(using: .utf8),
          let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let perKey = root["per_key"] as? [String: Any] else {
        return info
    }
    info.routingEnabled = jsonBool(root["enabled"]) ?? false
    info.configured = (root["configured"] as? NSNumber)?.intValue ?? perKey.count
    info.totalRemaining = (root["total_remaining"] as? NSNumber)?.doubleValue ?? 0
    info.usageUnknownCount = (root["usage_unknown_count"] as? NSNumber)?.intValue ?? 0

    for name in perKey.keys.sorted() {
        guard let rec = perKey[name] as? [String: Any] else { continue }
        var k = PoolKeyInfo()
        k.name = name
        k.remaining = (rec["remaining"] as? NSNumber)?.doubleValue
        k.spent = (rec["spent"] as? NSNumber)?.doubleValue
        k.enabled = jsonBool(rec["enabled"]) ?? true
        k.dead = jsonBool(rec["dead"]) ?? false
        k.unanchored = jsonBool(rec["unanchored"]) ?? false
        k.usageUnknown = jsonBool(rec["usage_unknown"]) ?? false
        k.recalibrated = rec["recalibrated"] as? String
        info.keys.append(k)
    }
    info.valid = true
    return info
}

func keyStateLabel(_ k: PoolKeyInfo) -> String {
    if k.dead { return "DEAD" }
    if !k.enabled { return "disabled" }
    if k.unanchored { return "unanchored" }
    if k.usageUnknown { return "usage unknown" }
    return ""
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var menu = NSMenu()
    var timer: Timer?
    var currentInfo = QuotaInfo()
    var currentPool = PoolInfo()

    // Named items so updates never depend on fixed indices.
    var remainingItem: NSMenuItem!
    var usedItem: NSMenuItem!
    var requestsItem: NSMenuItem!
    var updatedItem: NSMenuItem!
    var poolSubmenu: NSMenu!
    var poolHeaderItem: NSMenuItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "AR ..."

        menu.addItem(NSMenuItem(title: "AgentRouter Proxy", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())

        // Own account (cookie-based), unchanged.
        remainingItem = NSMenuItem(title: "Account: loading…", action: nil, keyEquivalent: "")
        usedItem = NSMenuItem(title: "Used: —", action: nil, keyEquivalent: "")
        requestsItem = NSMenuItem(title: "Requests: —", action: nil, keyEquivalent: "")
        updatedItem = NSMenuItem(title: "Updated: —", action: nil, keyEquivalent: "")
        menu.addItem(remainingItem)
        menu.addItem(usedItem)
        menu.addItem(requestsItem)
        menu.addItem(updatedItem)

        // Pool submenu, hidden until keys configure themselves.
        poolHeaderItem = NSMenuItem(title: "Pool", action: nil, keyEquivalent: "")
        poolSubmenu = NSMenu()
        menu.addItem(poolHeaderItem)
        poolHeaderItem.submenu = poolSubmenu
        poolHeaderItem.isHidden = true

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

    func updateOwnAccount(_ info: QuotaInfo) {
        if info.valid {
            let pctStr = String(format: "%.1f%%", info.pct)
            remainingItem.title = "Account: \(pctStr) ($\(String(format: "%.2f", info.dollars)))"
            usedItem.title = "Used: $\(String(format: "%.2f", info.usedDollars))"
            requestsItem.title = "Requests: \(info.requests)"
            updatedItem.title = "Updated: \(info.timestamp)"
        } else {
            remainingItem.title = "Account: unavailable (cookie expired?)"
            usedItem.title = "Used: —"
            requestsItem.title = "Requests: —"
            updatedItem.title = "Updated: —"
        }
    }

    func updatePoolMenu(_ pool: PoolInfo) {
        poolSubmenu.removeAllItems()
        guard pool.valid, pool.hasKeys else {
            poolHeaderItem.isHidden = true
            return
        }
        poolHeaderItem.isHidden = false

        let routing = pool.routingEnabled ? "Routing: on" : "Routing: OFF — set POOL_ENABLED=1"
        poolSubmenu.addItem(NSMenuItem(title: routing, action: nil, keyEquivalent: ""))
        poolSubmenu.addItem(NSMenuItem(
            title: "Combined: $\(String(format: "%.2f", pool.totalRemaining)) (\(pool.keys.count) key(s))",
            action: nil, keyEquivalent: ""))
        if pool.usageUnknownCount > 0 {
            poolSubmenu.addItem(NSMenuItem(
                title: "\(pool.usageUnknownCount) key(s) with unknown usage",
                action: nil, keyEquivalent: ""))
        }
        poolSubmenu.addItem(NSMenuItem.separator())

        for k in pool.keys {
            var title = k.name
            if let r = k.remaining {
                title += String(format: "  $%.2f", r)
            } else {
                title += "  $?"
            }
            if let s = k.spent, s > 0 {
                title += String(format: "  (spent $%.2f)", s)
            }
            let state = keyStateLabel(k)
            if !state.isEmpty { title += "  [\(state)]" }
            poolSubmenu.addItem(NSMenuItem(title: title, action: nil, keyEquivalent: ""))
        }
    }

    @objc func refreshNow(_ sender: Any?) {
        currentInfo = fetchQuota()
        currentPool = fetchPool()
        updateOwnAccount(currentInfo)
        updatePoolMenu(currentPool)

        if currentPool.valid && currentPool.hasKeys {
            let dollars = String(format: "%.2f", currentPool.totalRemaining)
            let warn = (!currentPool.routingEnabled || currentPool.usageUnknownCount > 0) ? " !" : ""
            statusItem.button?.title = "AR $\(dollars)\(warn)"
        } else if currentInfo.valid {
            statusItem.button?.title = String(format: "AR %.1f%%", currentInfo.pct)
        } else {
            statusItem.button?.title = "AR \u{26A0}\u{FE0F}"
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
