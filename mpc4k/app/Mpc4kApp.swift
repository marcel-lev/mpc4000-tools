import SwiftUI
import AppKit
import AVFoundation
import UniformTypeIdentifiers

@main
struct Mpc4kApp: App {
    var body: some Scene {
        WindowGroup("MPC 4000 File Manager") {
            ContentView()
        }
    }
}

// MARK: - Model types

struct RemoteEntry: Equatable {
    let name: String
    let isFolder: Bool
    let size: Int
}

/// One visible row of the (partially expanded) tree. `dir` is the folder
/// path relative to the current browse root ("" = the root itself).
struct Row: Identifiable, Equatable {
    let entry: RemoteEntry
    let dir: String
    let depth: Int
    var path: String { dir.isEmpty ? entry.name : dir + "\\" + entry.name }
    var id: String { (entry.isFolder ? "d:" : "f:") + path }
}

struct DiskInfo: Identifiable, Equatable {
    let handle: Int
    let name: String
    let typeName: String
    var id: Int { handle }
}

// MARK: - Backend (python helper process, JSON lines)

final class Backend: ObservableObject {
    @Published var connected = false
    @Published var statusText = "Connecting to MPC 4000…"
    @Published var childrenByPath: [String: [RemoteEntry]] = [:]
    @Published var expandedPaths: Set<String> = []
    @Published var loadingPaths: Set<String> = []
    @Published var path: [String] = []          // folder components (browse root)
    @Published var ramMode = false
    @Published var disks: [DiskInfo] = []
    @Published var currentDisk: Int? = nil
    @Published var busy = false
    @Published var busyText = ""
    @Published var progressDone: Int = 0
    @Published var progressTotal: Int? = nil
    @Published var lastError: String? = nil

    private var process: Process?
    private var stdinPipe = Pipe()
    private var stdoutPipe = Pipe()
    private var lineBuffer = Data()
    private var pendingCompletion: (([String: Any]) -> Void)?
    private let queue = DispatchQueue(label: "mpc4k.backend")
    private var opQueue: [(req: [String: Any], label: String,
                           completion: ([String: Any]) -> Void)] = []
    private var inFlight = false

    struct DiskTree {
        var children: [String: [RemoteEntry]]
        var expanded: Set<String>
        var path: [String]
    }
    private var diskCache: [Int: DiskTree] = [:]

    var remoteDir: String { path.joined(separator: "\\") }

    /// Flattened visible rows of the tree, honoring expansion state.
    var rows: [Row] {
        var out: [Row] = []
        func walk(dir: String, depth: Int) {
            for e in childrenByPath[dir] ?? [] {
                let row = Row(entry: e, dir: dir, depth: depth)
                out.append(row)
                if e.isFolder && expandedPaths.contains(row.path) {
                    walk(dir: row.path, depth: depth + 1)
                }
            }
        }
        walk(dir: "", depth: 0)
        return out
    }

    func fullRemoteDir(_ dir: String) -> String {
        if dir.isEmpty { return remoteDir }
        return remoteDir.isEmpty ? dir : remoteDir + "\\" + dir
    }

    // -- process lifecycle -------------------------------------------------

    func start() {
        guard process == nil else { return }
        guard let script = Bundle.main.url(forResource: "mpc4k", withExtension: "py") else {
            statusText = "bundled mpc4k.py missing"
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [script.path, "serve"]
        stdinPipe = Pipe()
        stdoutPipe = Pipe()
        p.standardInput = stdinPipe
        p.standardOutput = stdoutPipe
        p.standardError = FileHandle.nullDevice

        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty else { return }
            self?.queue.async { self?.consume(data) }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.connected = false
                self?.statusText = "Helper exited — is python3/libusb available?"
                self?.process = nil
            }
        }
        do {
            try p.run()
            process = p
            connect()
        } catch {
            statusText = "Could not start helper: \(error.localizedDescription)"
        }
    }

    private func consume(_ data: Data) {
        lineBuffer.append(data)
        while let nl = lineBuffer.firstIndex(of: 0x0A) {
            let line = lineBuffer.prefix(upTo: nl)
            lineBuffer.removeSubrange(...nl)
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any]
            else { continue }
            if let event = obj["event"] as? String, event == "progress" {
                DispatchQueue.main.async {
                    self.progressDone = obj["done"] as? Int ?? 0
                    if let t = obj["total"] as? Int { self.progressTotal = t }
                }
                continue
            }
            let completion = pendingCompletion
            pendingCompletion = nil
            if let c = completion {
                DispatchQueue.main.async { c(obj) }
            }
        }
    }

    private func send(_ req: [String: Any], label: String,
                      completion: @escaping ([String: Any]) -> Void) {
        opQueue.append((req, label, completion))
        pump()
    }

    private func pump() {
        if process == nil { start() }
        guard !inFlight, let next = opQueue.first else { return }
        guard let data = try? JSONSerialization.data(withJSONObject: next.req) else {
            opQueue.removeFirst()
            return
        }
        inFlight = true
        busy = true
        busyText = next.label
        progressDone = 0
        progressTotal = nil
        queue.async {
            self.pendingCompletion = { obj in
                self.inFlight = false
                if !self.opQueue.isEmpty { self.opQueue.removeFirst() }
                self.busy = !self.opQueue.isEmpty
                if let ok = obj["ok"] as? Bool, !ok {
                    self.lastError = obj["error"] as? String ?? "unknown error"
                }
                next.completion(obj)
                self.pump()
            }
            self.stdinPipe.fileHandleForWriting.write(data + Data([0x0A]))
        }
    }

    // -- operations --------------------------------------------------------

    func connect() {
        send(["op": "connect"], label: "Connecting…") { obj in
            guard let ok = obj["ok"] as? Bool, ok,
                  let result = obj["result"] as? [String: Any] else {
                self.statusText = "Not connected — is the MPC on and plugged in?"
                self.connected = false
                return
            }
            let product = result["product"] as? String ?? "?"
            if let dlist = result["disks"] as? [[String: Any]] {
                let mapped = dlist.map {
                    (disk: DiskInfo(handle: $0["handle"] as? Int ?? 0,
                                    name: $0["name"] as? String ?? "disk",
                                    typeName: ($0["type"] as? Int == 1) ? "internal drive"
                                              : ($0["type"] as? Int == 3 ? "USB drive" : "disk")),
                     isInternal: $0["type"] as? Int == 1)
                }
                // internal drive first (it is also the default selection)
                self.disks = mapped.filter(\.isInternal).map(\.disk)
                           + mapped.filter { !$0.isInternal }.map(\.disk)
            }
            if let disk = result["disk"] as? [String: Any] {
                self.currentDisk = disk["handle"] as? Int
            }
            self.connected = true
            self.statusText = "Akai \(product)"
            self.refresh()
        }
    }

    static let ramKinds = ["Samples", "Programs", "Multis", "Songs"]
    static let ramExt = ["Samples": ".wav", "Programs": ".akp",
                         "Multis": ".akm", "Songs": ".mid"]

    private func stashCurrentTree() {
        guard !ramMode, let disk = currentDisk else { return }
        diskCache[disk] = DiskTree(children: childrenByPath,
                                   expanded: expandedPaths, path: path)
    }

    private func restoreTree(for disk: Int) -> Bool {
        guard let cached = diskCache[disk] else { return false }
        childrenByPath = cached.children
        expandedPaths = cached.expanded
        path = cached.path
        loadingPaths = []
        return true
    }

    func pickSource(_ tag: Int) {
        stashCurrentTree()
        if tag == -2 {
            ramMode = true
            path = []
            resetTree()
            loadRam()
        } else if tag == currentDisk {
            // back from RAM: no select_disk needed (re-selecting the
            // internal drive costs a ~50s firmware re-scan); restore the
            // cached tree instantly and refresh it in the background
            ramMode = false
            if !restoreTree(for: tag) {
                path = []
                resetTree()
            }
            refresh()
        } else {
            // real disk change: show the cached tree immediately (if any)
            // while select_disk runs — the internal drive takes ~50s
            ramMode = false
            if !restoreTree(for: tag) {
                path = []
                resetTree()
            }
            selectDisk(tag)
        }
    }

    func loadRam() {
        send(["op": "mem_list"], label: "Reading RAM…") { obj in
            guard let result = obj["result"] as? [String: Any] else { return }
            var tree: [String: [RemoteEntry]] = [:]
            var groups: [RemoteEntry] = []
            var expanded: Set<String> = []
            for kind in Backend.ramKinds {
                let names = result[kind] as? [String] ?? []
                groups.append(RemoteEntry(name: kind, isFolder: true, size: 0))
                tree[kind] = names.map {
                    RemoteEntry(name: $0, isFolder: false, size: 0)
                }
                if !names.isEmpty { expanded.insert(kind) }
            }
            tree[""] = groups
            self.childrenByPath = tree
            self.expandedPaths = expanded
            self.loadingPaths = []
        }
    }

    func selectDisk(_ handle: Int) {
        let slow = disks.first { $0.handle == handle }?.typeName == "internal drive"
        send(["op": "select_disk", "handle": handle],
             label: slow ? "Switching — the MPC needs ~1 min to mount the internal drive; showing the last known contents…"
                         : "Switching disk…") { obj in
            if obj["ok"] as? Bool == true {
                self.currentDisk = handle
                self.refresh()
            }
        }
    }

    func saveRam(toFolder folder: String, kind: String? = nil,
                 name: String? = nil, completion: @escaping (Bool) -> Void) {
        var req: [String: Any] = ["op": "mem_save", "dir": folder]
        if let kind, let name {
            req["kind"] = kind
            req["name"] = name
        }
        send(req, label: name != nil ? "Saving \(name!) to disk…"
                                     : "Saving RAM to disk…") { obj in
            completion(obj["ok"] as? Bool == true)
        }
    }

    private static func parseEntries(_ obj: [String: Any]) -> [RemoteEntry]? {
        guard let result = obj["result"] as? [String: Any] else { return nil }
        var out: [RemoteEntry] = []
        for f in result["folders"] as? [String] ?? [] {
            out.append(RemoteEntry(name: f, isFolder: true, size: 0))
        }
        for f in result["files"] as? [[String: Any]] ?? [] {
            out.append(RemoteEntry(name: f["name"] as? String ?? "?",
                                   isFolder: false,
                                   size: f["size"] as? Int ?? 0))
        }
        return out
    }

    func refresh() {
        if ramMode {
            loadRam()
            return
        }
        send(["op": "ls", "path": remoteDir], label: "Reading folder…") { obj in
            self.childrenByPath = ["": Backend.parseEntries(obj) ?? []]
            self.loadingPaths = []
            self.reloadExpanded()
        }
    }

    /// Re-fetch children of still-existing expanded folders (top-down);
    /// prune expansion state for folders that disappeared.
    private func reloadExpanded() {
        for p in expandedPaths.sorted(by: { $0.count < $1.count }) {
            let comps = p.components(separatedBy: "\\")
            let parent = comps.dropLast().joined(separator: "\\")
            guard let siblings = childrenByPath[parent] else { continue }
            guard siblings.contains(where: { $0.isFolder && $0.name == comps.last }) else {
                expandedPaths.remove(p)
                childrenByPath[p] = nil
                continue
            }
            if childrenByPath[p] == nil && !loadingPaths.contains(p) {
                loadChildren(p)
                return  // serialized; continues from the completion
            }
        }
    }

    func loadChildren(_ p: String) {
        guard !ramMode, !loadingPaths.contains(p) else { return }
        loadingPaths.insert(p)
        let label = p.components(separatedBy: "\\").last ?? p
        send(["op": "ls", "path": fullRemoteDir(p)],
             label: "Reading \(label)…") { obj in
            self.loadingPaths.remove(p)
            if let entries = Backend.parseEntries(obj) {
                self.childrenByPath[p] = entries
            } else {
                self.expandedPaths.remove(p)
            }
            self.reloadExpanded()
        }
    }

    func toggleExpand(_ row: Row) {
        guard row.entry.isFolder else { return }
        if expandedPaths.contains(row.path) {
            expandedPaths.remove(row.path)
        } else {
            expandedPaths.insert(row.path)
            if childrenByPath[row.path] == nil {
                loadChildren(row.path)
            }
        }
    }

    private func resetTree() {
        childrenByPath = [:]
        expandedPaths = []
        loadingPaths = []
    }

    func enter(_ row: Row) {
        if ramMode {
            toggleExpand(row)
            return
        }
        path.append(contentsOf: row.path.components(separatedBy: "\\"))
        resetTree()
        refresh()
    }

    func goTo(index: Int) {   // -1 = root
        path = index < 0 ? [] : Array(path.prefix(index + 1))
        resetTree()
        refresh()
    }

    func mkdir(_ name: String) {
        send(["op": "mkdir", "dir": remoteDir, "name": name],
             label: "Creating folder…") { _ in self.refresh() }
    }

    func delete(_ row: Row, completion: (() -> Void)? = nil) {
        send(["op": "rm", "dir": fullRemoteDir(row.dir), "name": row.entry.name],
             label: "Deleting \(row.entry.name)…") { _ in
            completion?()
            self.refresh()
        }
    }

    func rename(_ row: Row, to newName: String) {
        send(["op": "rename", "dir": fullRemoteDir(row.dir), "old": row.entry.name,
              "new": newName, "is_folder": row.entry.isFolder],
             label: "Renaming…") { _ in self.refresh() }
    }

    func upload(_ urls: [URL]) {
        let files = urls.filter { !$0.hasDirectoryPath }
        guard !files.isEmpty else { return }
        uploadNext(files, index: 0)
    }

    private func uploadNext(_ files: [URL], index: Int) {
        guard index < files.count else {
            refresh()
            return
        }
        let url = files[index]
        send(["op": "put", "dir": remoteDir, "local": url.path,
              "name": url.lastPathComponent],
             label: "Uploading \(url.lastPathComponent) (\(index + 1)/\(files.count))…") { _ in
            self.uploadNext(files, index: index + 1)
        }
    }

    func download(_ rows: [Row], to dir: URL) {
        let files = rows.filter { !$0.entry.isFolder }
        guard !files.isEmpty else { return }
        downloadNext(files, index: 0, dir: dir)
    }

    private func downloadNext(_ files: [Row], index: Int, dir: URL) {
        guard index < files.count else { return }
        let f = files[index]
        let req: [String: Any]
        let local: String
        if ramMode {
            local = dir.appendingPathComponent(
                f.entry.name + (Backend.ramExt[f.dir] ?? "")).path
            req = ["op": "mem_get", "kind": f.dir, "name": f.entry.name,
                   "local": local]
        } else {
            local = dir.appendingPathComponent(f.entry.name).path
            req = ["op": "get", "dir": fullRemoteDir(f.dir),
                   "name": f.entry.name, "local": local]
        }
        send(req, label: "Downloading \(f.entry.name) (\(index + 1)/\(files.count))…") { _ in
            self.progressTotal = f.entry.size
            self.downloadNext(files, index: index + 1, dir: dir)
        }
    }

    func fetchForPreview(_ row: Row, to local: URL,
                         completion: @escaping (Bool) -> Void) {
        let req: [String: Any] = ramMode
            ? ["op": "mem_get", "kind": row.dir, "name": row.entry.name,
               "local": local.path]
            : ["op": "get", "dir": fullRemoteDir(row.dir),
               "name": row.entry.name, "local": local.path]
        send(req, label: "Loading \(row.entry.name)…") { obj in
            completion(obj["ok"] as? Bool == true)
        }
    }
}

// MARK: - Audio preview

final class AudioPreview: NSObject, ObservableObject, AVAudioPlayerDelegate {
    @Published var playingId: String? = nil
    @Published var loadingId: String? = nil
    private var player: AVAudioPlayer?

    static let cacheDir: URL = {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("mpc4k-preview")
        try? FileManager.default.createDirectory(at: dir,
                                                 withIntermediateDirectories: true)
        return dir
    }()

    static func isAudio(_ name: String) -> Bool {
        ["wav", "aif", "aiff"].contains((name as NSString).pathExtension.lowercased())
    }

    func cachePath(dir: String, entry: RemoteEntry) -> URL {
        let key = "\(dir)|\(entry.name)|\(entry.size)".unicodeScalars
            .reduce(into: UInt64(5381)) { $0 = $0 &* 127 &+ UInt64($1.value) }
        return AudioPreview.cacheDir
            .appendingPathComponent("\(key)-\(entry.name)")
    }

    func toggle(row: Row, backend: Backend) {
        if playingId == row.id {
            stop()
            return
        }
        stop()
        let local = cachePath(dir: backend.fullRemoteDir(row.dir), entry: row.entry)
        if FileManager.default.fileExists(atPath: local.path) {
            play(url: local, id: row.id)
            return
        }
        loadingId = row.id
        backend.fetchForPreview(row, to: local) { [weak self] ok in
            guard let self else { return }
            self.loadingId = nil
            if ok { self.play(url: local, id: row.id) }
        }
    }

    private func play(url: URL, id: String) {
        do {
            let p = try AVAudioPlayer(contentsOf: url)
            p.delegate = self
            p.play()
            player = p
            playingId = id
        } catch {
            playingId = nil
        }
    }

    func stop() {
        player?.stop()
        player = nil
        playingId = nil
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer,
                                     successfully flag: Bool) {
        DispatchQueue.main.async {
            self.playingId = nil
            self.player = nil
        }
    }
}

// MARK: - Views

struct ContentView: View {
    @StateObject private var backend = Backend()
    @StateObject private var preview = AudioPreview()
    @State private var selection = Set<String>()
    @State private var dropTargeted = false
    @State private var showNewFolder = false
    @State private var newFolderName = ""
    @State private var renameTarget: Row? = nil
    @State private var renameText = ""
    @State private var deleteTargets: [Row] = []
    @State private var showSaveRam = false
    @State private var saveRamItem: Row? = nil
    @State private var saveRamPath = ""

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            breadcrumbs
            Divider()
            fileTable
            Divider()
            statusBar
        }
        .frame(minWidth: 640, minHeight: 460)
        .onAppear { backend.start() }
        .alert("Error", isPresented: Binding(
            get: { backend.lastError != nil },
            set: { if !$0 { backend.lastError = nil } })) {
            Button("OK") { backend.lastError = nil }
        } message: {
            Text(backend.lastError ?? "")
        }
        .sheet(isPresented: $showNewFolder) { newFolderSheet }
        .sheet(isPresented: $showSaveRam) { saveRamSheet }
        .sheet(item: $renameTarget) { row in renameSheet(row) }
        .confirmationDialog(deleteMessage,
                            isPresented: Binding(
                                get: { !deleteTargets.isEmpty },
                                set: { if !$0 { deleteTargets = [] } }),
                            titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                let targets = deleteTargets
                deleteTargets = []
                for t in targets { backend.delete(t) }
                selection.removeAll()
            }
            Button("Cancel", role: .cancel) { deleteTargets = [] }
        }
    }

    private var deleteMessage: String {
        if deleteTargets.count == 1 {
            let t = deleteTargets[0]
            return t.entry.isFolder
                ? "Delete folder \"\(t.entry.name)\" and everything in it?"
                : "Delete \"\(t.entry.name)\"?"
        }
        return "Delete \(deleteTargets.count) items? Folders are deleted with all contents."
    }

    // -- header / toolbar --------------------------------------------------

    private var header: some View {
        HStack(spacing: 12) {
            Circle()
                .fill(backend.connected ? Color.green : Color.orange)
                .frame(width: 9, height: 9)
            Text(backend.statusText).font(.headline)
            if backend.connected {
                Picker("", selection: Binding(
                    get: { backend.ramMode ? -2 : (backend.currentDisk ?? -1) },
                    set: { backend.pickSource($0) })) {
                    ForEach(backend.disks) { d in
                        Text(d.typeName).tag(d.handle)
                    }
                    Text("RAM").tag(-2)
                }
                .pickerStyle(.segmented)
                .frame(width: min(320, CGFloat(backend.disks.count + 1) * 110))
                .disabled(backend.busy)
            }
            Spacer()
            Button { togglePreviewSelected() } label: {
                Image(systemName: preview.playingId != nil
                      ? "stop.fill" : "play.fill")
            }
                .help(preview.playingId != nil ? "Stop playback"
                      : "Play selected audio file (Space)")
                .keyboardShortcut(.space, modifiers: [])
                .disabled(preview.playingId == nil &&
                          (backend.busy || previewableSelection == nil))
            Divider().frame(height: 16)
            Button {
                backend.connected ? backend.refresh() : backend.connect()
            } label: { Image(systemName: "arrow.clockwise") }
                .help("Refresh")
                .disabled(backend.busy)
            if backend.ramMode {
                Button { showSaveRam = true; saveRamItem = nil }
                    label: { Image(systemName: "externaldrive.badge.checkmark") }
                    .help("Save everything in RAM to a folder on the MPC disk")
                    .disabled(backend.busy)
            }
            Button { showNewFolder = true; newFolderName = "" }
                label: { Image(systemName: "folder.badge.plus") }
                .help("New folder")
                .disabled(!backend.connected || backend.busy || backend.ramMode)
            Button { chooseUpload() }
                label: { Image(systemName: "square.and.arrow.up") }
                .help("Upload files to this folder")
                .disabled(!backend.connected || backend.busy || backend.ramMode)
            Button { chooseDownload() }
                label: { Image(systemName: "square.and.arrow.down") }
                .help("Download selected files")
                .disabled(!backend.connected || backend.busy || selectedFiles.isEmpty)
            Button { startRename() }
                label: { Image(systemName: "pencil") }
                .help("Rename")
                .disabled(!backend.connected || backend.busy || backend.ramMode
                          || selectedEntries.count != 1)
            Button { deleteTargets = selectedEntries }
                label: { Image(systemName: "trash") }
                .help("Delete")
                .disabled(!backend.connected || backend.busy || backend.ramMode
                          || selectedEntries.isEmpty)
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var breadcrumbs: some View {
        HStack(spacing: 4) {
            Button(backend.ramMode ? "RAM" : "Internal Drive") { backend.goTo(index: -1) }
                .buttonStyle(.link)
                .disabled(backend.busy)
            ForEach(Array(backend.path.enumerated()), id: \.offset) { i, comp in
                Image(systemName: "chevron.right")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Button(comp) { backend.goTo(index: i) }
                    .buttonStyle(.link)
                    .disabled(backend.busy)
            }
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
    }

    // -- file table --------------------------------------------------------

    private var fileTable: some View {
        Table(backend.rows, selection: $selection) {
            TableColumn("Name") { row in
                let entry = row.entry
                HStack(spacing: 4) {
                    Spacer().frame(width: CGFloat(row.depth) * 18)
                    if entry.isFolder {
                        Button {
                            backend.toggleExpand(row)
                        } label: {
                            if backend.loadingPaths.contains(row.path) {
                                ProgressView().controlSize(.mini)
                            } else {
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 10, weight: .semibold))
                                    .foregroundStyle(.secondary)
                                    .rotationEffect(.degrees(
                                        backend.expandedPaths.contains(row.path) ? 90 : 0))
                            }
                        }
                        .buttonStyle(.borderless)
                        .frame(width: 16)
                    } else {
                        Spacer().frame(width: 16)
                    }
                    Group {
                        if preview.playingId == row.id {
                            Image(systemName: "speaker.wave.2.fill")
                                .foregroundStyle(Color.accentColor)
                        } else if preview.loadingId == row.id {
                            ProgressView().controlSize(.mini)
                        } else {
                            Image(systemName: entry.isFolder ? "folder.fill"
                                  : iconFor(entry.name))
                                .foregroundStyle(entry.isFolder ? Color.accentColor
                                                 : .secondary)
                        }
                    }
                    Text(entry.name).padding(.leading, 2)
                }
                .contentShape(Rectangle())
                .onTapGesture(count: 2) {
                    if entry.isFolder {
                        backend.enter(row)
                    } else if canPreview(row) {
                        preview.toggle(row: row, backend: backend)
                    }
                }
            }
            TableColumn("Size") { row in
                Text(row.entry.isFolder || row.entry.size == 0 ? "—"
                     : humanSize(row.entry.size))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(90)
        }
        .contextMenu(forSelectionType: String.self) { ids in
            let items = backend.rows.filter { ids.contains($0.id) }
            if items.count == 1, items[0].entry.isFolder {
                Button("Open") { backend.enter(items[0]) }
                Button(backend.expandedPaths.contains(items[0].path)
                       ? "Collapse" : "Expand") {
                    backend.toggleExpand(items[0])
                }
            }
            if items.count == 1, !items[0].entry.isFolder, canPreview(items[0]) {
                Button(preview.playingId == items[0].id ? "Stop" : "Play") {
                    preview.toggle(row: items[0], backend: backend)
                }
            }
            if backend.ramMode, items.count == 1, !items[0].entry.isFolder {
                Button("Save to MPC disk…") {
                    saveRamItem = items[0]
                    showSaveRam = true
                }
            }
            if items.count == 1, !backend.ramMode {
                Button("Rename…") {
                    renameTarget = items[0]
                    renameText = items[0].entry.name
                }
            }
            if !items.filter({ !$0.entry.isFolder }).isEmpty {
                Button("Download…") { chooseDownload(items) }
            }
            if !items.isEmpty, !backend.ramMode {
                Button("Delete…", role: .destructive) { deleteTargets = items }
            }
        } primaryAction: { ids in
            guard let id = ids.first,
                  let row = backend.rows.first(where: { $0.id == id })
            else { return }
            if row.entry.isFolder {
                backend.enter(row)
            } else if canPreview(row) {
                preview.toggle(row: row, backend: backend)
            }
        }
        .overlay {
            if backend.rows.isEmpty && backend.connected && !backend.busy {
                Text("Empty folder — drop files here to upload")
                    .foregroundStyle(.secondary)
            }
            if dropTargeted {
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(Color.accentColor, lineWidth: 3)
                    .background(Color.accentColor.opacity(0.08))
                    .allowsHitTesting(false)
            }
        }
        .onDrop(of: [UTType.fileURL], isTargeted: $dropTargeted) { providers in
            var urls: [URL] = []
            let group = DispatchGroup()
            for provider in providers {
                group.enter()
                provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier,
                                  options: nil) { item, _ in
                    defer { group.leave() }
                    if let data = item as? Data,
                       let u = URL(dataRepresentation: data, relativeTo: nil) {
                        urls.append(u)
                    } else if let u = item as? URL {
                        urls.append(u)
                    }
                }
            }
            group.notify(queue: .main) {
                if backend.ramMode {
                    backend.lastError = "Switch to a disk to upload files — RAM is read-only here."
                } else if !urls.isEmpty {
                    backend.upload(urls)
                }
            }
            return true
        }
    }

    private var statusBar: some View {
        HStack(spacing: 10) {
            if backend.busy {
                ProgressView().controlSize(.small)
                Text(backend.busyText).font(.callout)
                if backend.progressDone > 0 {
                    Text(progressText).font(.callout).foregroundStyle(.secondary)
                }
            } else {
                let root = backend.childrenByPath[""] ?? []
                let folders = root.filter(\.isFolder).count
                let files = root.count - folders
                Text("\(folders) folders, \(files) files")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
    }

    private var progressText: String {
        if let total = backend.progressTotal, total > 0 {
            return "\(humanSize(backend.progressDone)) / \(humanSize(total))"
        }
        return humanSize(backend.progressDone)
    }

    // -- sheets ------------------------------------------------------------

    private var newFolderSheet: some View {
        VStack(spacing: 14) {
            Text("New folder in \(backend.path.last ?? "root")").font(.headline)
            TextField("Folder name", text: $newFolderName)
                .textFieldStyle(.roundedBorder)
                .frame(width: 260)
                .onSubmit { submitNewFolder() }
            HStack {
                Button("Cancel") { showNewFolder = false }
                Button("Create") { submitNewFolder() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(newFolderName.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(24)
    }

    private var saveRamSheet: some View {
        VStack(spacing: 14) {
            Text(saveRamItem != nil
                 ? "Save \"\(saveRamItem!.entry.name)\" to the MPC disk"
                 : "Save everything in RAM to the MPC disk")
                .font(.headline)
            Text("Saves to the last selected drive (internal by default). Programs are saved together with their samples; existing files are overwritten.")
                .font(.callout)
                .foregroundStyle(.secondary)
            TextField("Folder, e.g. 7 Recordings/Session1 (empty = root)",
                      text: $saveRamPath)
                .textFieldStyle(.roundedBorder)
                .frame(width: 380)
            HStack {
                Button("Cancel") { showSaveRam = false }
                Button("Save") {
                    let folder = saveRamPath
                        .trimmingCharacters(in: .whitespaces)
                        .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
                        .replacingOccurrences(of: "/", with: "\\")
                    let item = saveRamItem
                    showSaveRam = false
                    backend.saveRam(toFolder: folder,
                                    kind: item?.dir, name: item?.entry.name) { _ in }
                }
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
    }

    private func submitNewFolder() {
        let name = newFolderName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else { return }
        showNewFolder = false
        backend.mkdir(name)
    }

    private func renameSheet(_ row: Row) -> some View {
        VStack(spacing: 14) {
            Text("Rename \(row.entry.isFolder ? "folder" : "file")").font(.headline)
            TextField("New name", text: $renameText)
                .textFieldStyle(.roundedBorder)
                .frame(width: 280)
                .onSubmit { submitRename(row) }
            HStack {
                Button("Cancel") { renameTarget = nil }
                Button("Rename") { submitRename(row) }
                    .keyboardShortcut(.defaultAction)
                    .disabled(renameText.trimmingCharacters(in: .whitespaces).isEmpty
                              || renameText == row.entry.name)
            }
        }
        .padding(24)
    }

    private func submitRename(_ row: Row) {
        let name = renameText.trimmingCharacters(in: .whitespaces)
        renameTarget = nil
        guard !name.isEmpty, name != row.entry.name else { return }
        backend.rename(row, to: name)
    }

    // -- helpers -----------------------------------------------------------

    private var selectedEntries: [Row] {
        backend.rows.filter { selection.contains($0.id) }
    }

    private var selectedFiles: [Row] {
        selectedEntries.filter { !$0.entry.isFolder }
    }

    private func startRename() {
        guard let row = selectedEntries.first else { return }
        renameTarget = row
        renameText = row.entry.name
    }

    private func canPreview(_ row: Row) -> Bool {
        if row.entry.isFolder { return false }
        if backend.ramMode { return row.dir == "Samples" }
        return AudioPreview.isAudio(row.entry.name)
    }

    private var previewableSelection: Row? {
        let items = selectedEntries
        guard items.count == 1, let r = items.first, canPreview(r) else { return nil }
        return r
    }

    private func togglePreviewSelected() {
        if preview.playingId != nil {
            preview.stop()
        } else if let r = previewableSelection {
            preview.toggle(row: r, backend: backend)
        }
    }

    private func chooseUpload() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Upload"
        if panel.runModal() == .OK {
            backend.upload(panel.urls)
        }
    }

    private func chooseDownload(_ items: [Row]? = nil) {
        let files = (items ?? selectedEntries).filter { !$0.entry.isFolder }
        guard !files.isEmpty else { return }
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.prompt = "Download Here"
        if panel.runModal() == .OK, let dir = panel.url {
            backend.download(files, to: dir)
        }
    }

    private func iconFor(_ name: String) -> String {
        let ext = (name as NSString).pathExtension.lowercased()
        switch ext {
        case "wav", "aif", "aiff": return "waveform"
        case "akp", "pgm": return "pianokeys"
        case "akm": return "square.stack.3d.up"
        case "mid", "smf": return "music.note.list"
        default: return "doc"
        }
    }
}

func humanSize(_ n: Int) -> String {
    if n < 1024 { return "\(n) B" }
    let kb = Double(n) / 1024
    if kb < 1024 { return String(format: "%.1f KB", kb) }
    return String(format: "%.1f MB", kb / 1024)
}
