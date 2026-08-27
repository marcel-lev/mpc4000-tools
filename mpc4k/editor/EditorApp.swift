import SwiftUI
import AppKit

@main
struct EditorApp: App {
    var body: some Scene {
        WindowGroup("MPC 4000 Program Editor") {
            EditorRootView()
        }
    }
}

// MARK: - Backend (python helper, JSON lines, FIFO op queue)

final class EditorBackend: ObservableObject {
    @Published var connected = false
    @Published var statusText = "Connecting to MPC 4000…"
    @Published var busy = false
    @Published var busyText = ""
    @Published var lastError: String? = nil

    private var process: Process?
    private var stdinPipe = Pipe()
    private var stdoutPipe = Pipe()
    private var lineBuffer = Data()
    private var pendingCompletion: (([String: Any]) -> Void)?
    private let ioQueue = DispatchQueue(label: "mpc4k.editor")
    private var opQueue: [(req: [String: Any], label: String,
                           completion: ([String: Any]) -> Void)] = []
    private var inFlight = false

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
            self?.ioQueue.async { self?.consume(data) }
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.connected = false
                self?.statusText = "Helper exited"
                self?.process = nil
            }
        }
        do { try p.run(); process = p } catch {
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
            if obj["event"] as? String == "progress" { continue }
            let completion = pendingCompletion
            pendingCompletion = nil
            if let c = completion { DispatchQueue.main.async { c(obj) } }
        }
    }

    func send(_ req: [String: Any], label: String,
              completion: @escaping ([String: Any]) -> Void = { _ in }) {
        opQueue.append((req, label, completion))
        pump()
    }

    /// Replace any queued op with the same coalescing key (for slider drags).
    func sendCoalesced(key: String, _ req: [String: Any], label: String) {
        var r = req
        r["_coalesce"] = key
        if let i = opQueue.firstIndex(where: { ($0.req["_coalesce"] as? String) == key }),
           i > 0 || !inFlight {
            opQueue[i] = (r, label, { _ in })
        } else {
            opQueue.append((r, label, { _ in }))
        }
        pump()
    }

    private func pump() {
        if process == nil { start() }
        guard !inFlight, let next = opQueue.first else { return }
        var clean = next.req
        clean.removeValue(forKey: "_coalesce")
        guard let data = try? JSONSerialization.data(withJSONObject: clean) else {
            opQueue.removeFirst()
            return
        }
        inFlight = true
        busy = true
        busyText = next.label
        ioQueue.async {
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
}

// MARK: - Model

struct KeygroupSummary: Identifiable {
    let index: Int
    var low: Int
    var high: Int
    var id: Int { index }
}

func noteName(_ n: Int) -> String {
    let names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return "\(names[n % 12])\(n / 12 - 2)"
}

let FILTER_TYPES = ["Off", "2-Pole LP", "2-Pole LP+", "4-Pole LP", "4-Pole LP+",
                    "6-Pole LP", "2-Pole BP", "2-Pole BP+", "4-Pole BP",
                    "4-Pole BP+", "6-Pole BP", "1-Pole HP", "1-Pole HP+",
                    "2-Pole HP", "2-Pole HP+", "6-Pole HP", "LO<>HI",
                    "LO<>BAND", "BAND<>HI", "Notch 1", "Notch 2", "Notch 3",
                    "Wide Notch", "Bi-Notch", "Peak 1", "Peak 2", "Peak 3",
                    "Wide Peak", "Bi-Peak", "Phaser 1", "Phaser 2", "Bi-Phase",
                    "Voweliser", "Triple"]
let LFO_WAVES = ["Triangle", "Sine", "Square", "Saw Up", "Saw Down", "Random"]
let PLAYBACK_MODES = ["No Loop", "One Shot", "Loop in Rel", "Loop until Rel",
                      "LIR-Retrig", "Play-Retrig", "As Sample"]
let SAMPLE_MODES = ["No Looping", "Looping", "One Shot"]

final class EditorModel: ObservableObject {
    let backend = EditorBackend()

    @Published var programs: [String] = []
    @Published var currentProgram: String? = nil
    @Published var keygroups: [KeygroupSummary] = []
    @Published var progLevel: Double = 0
    @Published var progTune: Double = 0
    @Published var progPolyphony: Double = 64
    @Published var pbUp: Double = 2
    @Published var pbDown: Double = 2

    @Published var kgIndex: Int = 0
    @Published var kg: [String: Any] = [:]
    @Published var editAll = false

    @Published var sampleInfo: [String: Any] = [:]
    @Published var samplePeaks: [Float] = []
    @Published var sampleFrames: Int = 0
    @Published var auditioning = false

    func start() {
        backend.start()
        refreshPrograms()
    }

    func refreshPrograms() {
        backend.send(["op": "mem_list"], label: "Reading RAM…") { obj in
            let result = obj["result"] as? [String: Any]
            self.programs = result?["Programs"] as? [String] ?? []
            self.backend.connected = obj["ok"] as? Bool == true
            self.backend.statusText = self.backend.connected
                ? "Akai MPC4000" : "Not connected — is the MPC on?"
        }
    }

    func openProgram(_ name: String) {
        currentProgram = name
        backend.send(["op": "prog_open", "name": name],
                     label: "Opening \(name)…") { obj in
            guard let r = obj["result"] as? [String: Any] else { return }
            self.progLevel = r["level"] as? Double ?? Double(r["level"] as? Int ?? 0)
            self.progTune = Double(r["tune"] as? Int ?? 0)
            self.progPolyphony = Double(r["polyphony"] as? Int ?? 64)
            self.pbUp = Double(r["pb_up"] as? Int ?? 2)
            self.pbDown = Double(r["pb_down"] as? Int ?? 2)
            let kgs = (r["keygroups"] as? [[String: Any]] ?? []).map {
                KeygroupSummary(index: $0["index"] as? Int ?? 0,
                                low: $0["low"] as? Int ?? 0,
                                high: $0["high"] as? Int ?? 127)
            }
            self.keygroups = kgs
            self.selectKeygroup(0)
        }
    }

    func selectKeygroup(_ i: Int) {
        guard let prog = currentProgram, i >= 0, i < max(keygroups.count, 1) else { return }
        kgIndex = i
        backend.send(["op": "kg_get", "prog": prog, "index": i],
                     label: "Reading keygroup \(i + 1)…") { obj in
            guard let r = obj["result"] as? [String: Any] else { return }
            self.kg = r
            if let zones = r["zones"] as? [[String: Any]],
               let z1 = zones.first, let s = z1["sample"] as? String, !s.isEmpty {
                self.loadSample(s)
            } else {
                self.sampleInfo = [:]
                self.samplePeaks = []
            }
        }
    }

    func setProgram(_ param: String, _ value: Any, display: String) {
        guard let prog = currentProgram else { return }
        backend.sendCoalesced(key: "prog.\(param)",
                              ["op": "prog_set", "name": prog,
                               "param": param, "value": value],
                              label: display)
    }

    func setKeygroup(_ param: String, _ value: Any, zone: Int? = nil,
                     display: String? = nil) {
        guard let prog = currentProgram else { return }
        var req: [String: Any] = ["op": "kg_set", "prog": prog,
                                  "index": kgIndex, "param": param,
                                  "value": value, "edit_all": editAll]
        if let z = zone { req["zone"] = z }
        let key = "kg.\(kgIndex).\(zone.map(String.init) ?? "-").\(param)"
        backend.sendCoalesced(key: key, req,
                              label: display ?? "Setting \(param)…")
        if param == "low" || param == "high",
           let idx = keygroups.firstIndex(where: { $0.index == kgIndex }),
           let v = value as? Int {
            if param == "low" { keygroups[idx].low = v } else { keygroups[idx].high = v }
        }
    }

    // -- sample handling ---------------------------------------------------

    var currentSampleName: String? {
        (kg["zones"] as? [[String: Any]])?.first?["sample"] as? String
    }

    func loadSample(_ name: String) {
        backend.send(["op": "sample_info", "name": name],
                     label: "Reading sample…") { obj in
            guard let r = obj["result"] as? [String: Any] else { return }
            self.sampleInfo = r
            self.downloadWaveform(name)
        }
    }

    private func downloadWaveform(_ name: String) {
        let cache = FileManager.default.temporaryDirectory
            .appendingPathComponent("mpc4k-editor")
        try? FileManager.default.createDirectory(at: cache,
                                                 withIntermediateDirectories: true)
        let local = cache.appendingPathComponent(name + ".wav")
        let finish = { self.decodePeaks(local) }
        if FileManager.default.fileExists(atPath: local.path) {
            finish()
            return
        }
        backend.send(["op": "mem_get", "kind": "Samples", "name": name,
                      "local": local.path],
                     label: "Loading waveform…") { obj in
            if obj["ok"] as? Bool == true { finish() }
        }
    }

    private func decodePeaks(_ url: URL) {
        guard let data = try? Data(contentsOf: url), data.count > 44 else { return }
        // minimal RIFF walk for fmt + data
        var ch = 1, bits = 16, pcm = Data()
        var pos = 12
        while pos + 8 <= data.count {
            let cid = String(data: data[pos..<pos+4], encoding: .ascii) ?? ""
            let csz = data.subdata(in: pos+4..<pos+8).withUnsafeBytes {
                Int($0.load(as: UInt32.self))
            }
            let body = data.subdata(in: pos+8..<min(pos+8+csz, data.count))
            if cid == "fmt ", body.count >= 16 {
                ch = Int(body[2]) | (Int(body[3]) << 8)
                bits = Int(body[14]) | (Int(body[15]) << 8)
            } else if cid == "data" {
                pcm = body
            }
            pos += 8 + csz + (csz & 1)
        }
        let bytesPer = bits / 8
        let frameSize = ch * bytesPer
        guard frameSize > 0 else { return }
        let frames = pcm.count / frameSize
        let buckets = 800
        var peaks = [Float](repeating: 0, count: buckets)
        pcm.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            let p = raw.bindMemory(to: UInt8.self).baseAddress!
            for b in 0..<buckets {
                let f0 = b * frames / buckets
                let f1 = max(f0 + 1, (b + 1) * frames / buckets)
                var peak: Float = 0
                var f = f0
                let step = max(1, (f1 - f0) / 64)
                while f < f1 {
                    let off = f * frameSize
                    var v: Int32 = 0
                    if bytesPer == 2 {
                        v = Int32(Int16(littleEndian:
                            Int16(bitPattern: UInt16(p[off]) | (UInt16(p[off+1]) << 8))))
                        v <<= 8
                    } else if bytesPer == 3 {
                        v = Int32(p[off]) | (Int32(p[off+1]) << 8) | (Int32(p[off+2]) << 16)
                        if v & 0x800000 != 0 { v -= 0x1000000 }
                    }
                    peak = max(peak, abs(Float(v)) / 8388608.0)
                    f += step
                }
                peaks[b] = peak
            }
        }
        DispatchQueue.main.async {
            self.samplePeaks = peaks
            self.sampleFrames = frames
        }
    }

    func setSample(_ param: String, _ value: Int) {
        guard let name = currentSampleName, !name.isEmpty else { return }
        sampleInfo[param] = value
        backend.sendCoalesced(key: "smp.\(param)",
                              ["op": "sample_set", "name": name,
                               "param": param, "value": value],
                              label: "Setting \(param)…")
    }

    func audition(loop: Bool) {
        guard let name = currentSampleName, !name.isEmpty else { return }
        if auditioning {
            backend.send(["op": "sample_stop"], label: "Stop") { _ in }
            auditioning = false
        } else {
            backend.send(["op": "sample_play", "name": name,
                          "velocity": 110, "loop": loop],
                         label: "Playing on MPC…") { _ in }
            auditioning = true
        }
    }

    func saveToDisk() {
        guard let prog = currentProgram else { return }
        backend.send(["op": "mem_save", "dir": "", "kind": "Programs",
                      "name": prog],
                     label: "Saving \(prog) to disk (with samples)…") { _ in }
    }
}

// MARK: - Root view

struct EditorRootView: View {
    @StateObject private var model = EditorModel()

    var body: some View {
        NavigationSplitView {
            programList
        } detail: {
            if model.currentProgram != nil {
                editorPane
            } else {
                Text("Select a program from RAM")
                    .foregroundStyle(.secondary)
            }
        }
        .frame(minWidth: 980, minHeight: 700)
        .onAppear { model.start() }
        .alert("Error", isPresented: Binding(
            get: { model.backend.lastError != nil },
            set: { if !$0 { model.backend.lastError = nil } })) {
            Button("OK") { model.backend.lastError = nil }
        } message: { Text(model.backend.lastError ?? "") }
    }

    private var programList: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Circle().fill(model.backend.connected ? .green : .orange)
                    .frame(width: 8, height: 8)
                Text(model.backend.statusText).font(.callout).bold()
                Spacer()
                Button { model.refreshPrograms() } label: {
                    Image(systemName: "arrow.clockwise")
                }.buttonStyle(.borderless)
            }
            .padding(10)
            Divider()
            List(model.programs, id: \.self,
                 selection: Binding(get: { model.currentProgram },
                                    set: { if let n = $0 { model.openProgram(n) } })) { name in
                Label(name, systemImage: "pianokeys").tag(name)
            }
        }
        .navigationSplitViewColumnWidth(min: 200, ideal: 230)
    }

    private var editorPane: some View {
        VStack(spacing: 0) {
            programHeader
            Divider()
            keygroupStrip
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    HStack(alignment: .top, spacing: 14) {
                        filterBox
                        ampEnvBox
                        filterEnvBox
                    }
                    HStack(alignment: .top, spacing: 14) {
                        tuneBox
                        lfoBox
                        zonesBox
                    }
                    waveformBox
                }
                .padding(14)
            }
            Divider()
            statusBar
        }
    }

    // -- header ------------------------------------------------------------

    private var programHeader: some View {
        HStack(spacing: 16) {
            Text(model.currentProgram ?? "").font(.title3).bold()
            slider("Level", value: $model.progLevel, in: -60...6, unit: "dB") {
                model.setProgram("level", $0, display: "Program level")
            }
            .frame(width: 200)
            slider("Tune", value: $model.progTune, in: -3600...3600, unit: "ct") {
                model.setProgram("tune", Int($0), display: "Program tune")
            }
            .frame(width: 200)
            Spacer()
            Toggle(isOn: $model.editAll) {
                Label("Edit All Keygroups", systemImage: "square.stack.3d.up.fill")
            }
            .toggleStyle(.button)
            .tint(.orange)
            .help("Apply keygroup edits to ALL keygroups of the program (MPC EDIT ALL)")
            Button {
                model.saveToDisk()
            } label: { Label("Save to Disk", systemImage: "externaldrive.badge.checkmark") }
                .help("Save the program + its samples to the MPC's current disk folder")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    private var keygroupStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(model.keygroups) { kg in
                    Button {
                        model.selectKeygroup(kg.index)
                    } label: {
                        VStack(spacing: 2) {
                            Text("KG \(kg.index + 1)").font(.caption).bold()
                            Text("\(noteName(kg.low))–\(noteName(kg.high))")
                                .font(.caption2)
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(RoundedRectangle(cornerRadius: 7)
                            .fill(kg.index == model.kgIndex
                                  ? Color.accentColor.opacity(0.35)
                                  : Color.primary.opacity(0.06)))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
        }
    }

    // -- parameter boxes ---------------------------------------------------

    private func kgInt(_ key: String, _ def: Int = 0) -> Int {
        model.kg[key] as? Int ?? def
    }

    private func box<Content: View>(_ title: String,
                                    @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.headline)
            content()
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
    }

    private var filterBox: some View {
        box("Filter") {
            Picker("Type", selection: Binding(
                get: { kgInt("filter_mode") },
                set: { model.kg["filter_mode"] = $0
                       model.setKeygroup("filter_mode", $0) })) {
                ForEach(0..<FILTER_TYPES.count, id: \.self) { i in
                    Text(FILTER_TYPES[i]).tag(i)
                }
            }
            kgSlider("Cutoff", "filter_cutoff", 0...100)
            kgSlider("Resonance", "filter_res", 0...60)
        }
    }

    private var ampEnvBox: some View {
        box("Amp Envelope") {
            kgSlider("Attack", "amp_attack", 0...100)
            kgSlider("Decay", "amp_decay", 0...100)
            kgSlider("Sustain", "amp_sustain", 0...100)
            kgSlider("Release", "amp_release", 0...100)
        }
    }

    private var filterEnvBox: some View {
        box("Filter Envelope") {
            envRow("R1 (Att)", "fenv_rate1", ratesIndex: 0)
            envRow("L1", "fenv_level1", levelsIndex: 0)
            envRow("R2", "fenv_rate2", ratesIndex: 1)
            envRow("L2", "fenv_level2", levelsIndex: 1)
            envRow("R4 (Rel)", "fenv_rate4", ratesIndex: 3)
        }
    }

    private func envRow(_ label: String, _ param: String,
                        ratesIndex: Int? = nil, levelsIndex: Int? = nil) -> some View {
        let arr = (ratesIndex != nil ? model.kg["fenv_rates"] : model.kg["fenv_levels"])
            as? [Int] ?? [0, 0, 0, 0]
        let idx = ratesIndex ?? levelsIndex ?? 0
        return HStack {
            Text(label).font(.caption).frame(width: 62, alignment: .leading)
            Slider(value: Binding(
                get: { Double(arr.indices.contains(idx) ? arr[idx] : 0) },
                set: { v in
                    var a = arr
                    if a.indices.contains(idx) { a[idx] = Int(v) }
                    model.kg[ratesIndex != nil ? "fenv_rates" : "fenv_levels"] = a
                    model.setKeygroup(param, Int(v))
                }), in: 0...100)
            Text("\(arr.indices.contains(idx) ? arr[idx] : 0)")
                .font(.caption.monospacedDigit()).frame(width: 30)
        }
    }

    private var tuneBox: some View {
        box("Keygroup") {
            HStack {
                Text("Range").font(.caption).frame(width: 62, alignment: .leading)
                Stepper("\(noteName(kgInt("low")))",
                        value: Binding(get: { kgInt("low") },
                                       set: { model.kg["low"] = $0
                                              model.setKeygroup("low", $0) }),
                        in: 0...127)
                Stepper("\(noteName(kgInt("high", 127)))",
                        value: Binding(get: { kgInt("high", 127) },
                                       set: { model.kg["high"] = $0
                                              model.setKeygroup("high", $0) }),
                        in: 0...127)
            }
            kgSlider("Tune ct", "tune", -3600...3600)
            HStack {
                Text("Mute grp").font(.caption).frame(width: 62, alignment: .leading)
                Stepper("\(kgInt("mute_group"))",
                        value: Binding(get: { kgInt("mute_group") },
                                       set: { model.kg["mute_group"] = $0
                                              model.setKeygroup("mute_group", $0) }),
                        in: 0...64)
            }
        }
    }

    private var lfoBox: some View {
        box("LFO 1") {
            Picker("Wave", selection: Binding(
                get: { kgInt("lfo1_wave") },
                set: { model.kg["lfo1_wave"] = $0
                       model.setKeygroup("lfo1_wave", $0) })) {
                ForEach(0..<LFO_WAVES.count, id: \.self) { i in
                    Text(LFO_WAVES[i]).tag(i)
                }
            }
            kgSlider("Rate", "lfo1_rate", 0...100)
            kgSlider("Depth", "lfo1_depth", 0...100)
        }
    }

    private var zonesBox: some View {
        box("Zones") {
            let zones = model.kg["zones"] as? [[String: Any]] ?? []
            ForEach(0..<max(zones.count, 1), id: \.self) { zi in
                if zones.indices.contains(zi) {
                    let z = zones[zi]
                    let name = z["sample"] as? String ?? ""
                    if !name.isEmpty {
                        HStack(spacing: 6) {
                            Text("Z\(zi + 1)").font(.caption).bold()
                            Text(name).font(.caption)
                                .lineLimit(1)
                            Spacer()
                            Text("vel \(z["vel_low"] as? Int ?? 0)–\(z["vel_high"] as? Int ?? 127)")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            if zones.allSatisfy({ ($0["sample"] as? String ?? "").isEmpty }) {
                Text("No samples assigned").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // -- waveform ----------------------------------------------------------

    private var waveformBox: some View {
        box("Sample — \(model.currentSampleName ?? "none")") {
            if model.samplePeaks.isEmpty {
                Text("No sample loaded for this keygroup")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 80)
            } else {
                WaveformEditor(model: model)
                    .frame(height: 170)
                HStack(spacing: 14) {
                    Button {
                        model.audition(loop: false)
                    } label: {
                        Label(model.auditioning ? "Stop" : "Play on MPC",
                              systemImage: model.auditioning ? "stop.fill" : "play.fill")
                    }
                    Button { model.audition(loop: true) } label: {
                        Label("Play Looped", systemImage: "repeat")
                    }.disabled(model.auditioning)
                    Picker("Mode", selection: Binding(
                        get: { model.sampleInfo["playback_mode"] as? Int ?? 0 },
                        set: { model.setSample("playback_mode", $0) })) {
                        ForEach(0..<SAMPLE_MODES.count, id: \.self) { i in
                            Text(SAMPLE_MODES[i]).tag(i)
                        }
                    }.frame(width: 220)
                    Spacer()
                    Text(sampleMeta).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private var sampleMeta: String {
        let len = model.sampleInfo["length"] as? Int ?? 0
        let rate = model.sampleInfo["rate"] as? Int ?? 44100
        return String(format: "%d frames · %.2fs · %dHz",
                      len, Double(len) / Double(max(rate, 1)), rate)
    }

    private var statusBar: some View {
        HStack(spacing: 8) {
            if model.backend.busy {
                ProgressView().controlSize(.small)
                Text(model.backend.busyText).font(.callout)
            } else {
                Text(model.editAll ? "EDIT ALL active — keygroup edits affect the whole program"
                                   : "Edits are applied live to the MPC")
                    .font(.callout)
                    .foregroundStyle(model.editAll ? .orange : .secondary)
            }
            Spacer()
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
    }

    // -- small helpers -----------------------------------------------------

    private func kgSlider(_ label: String, _ param: String,
                          _ range: ClosedRange<Double>) -> some View {
        HStack {
            Text(label).font(.caption).frame(width: 62, alignment: .leading)
            Slider(value: Binding(
                get: { Double(kgInt(param)) },
                set: { v in
                    model.kg[param] = Int(v)
                    model.setKeygroup(param, Int(v))
                }), in: range)
            Text("\(kgInt(param))").font(.caption.monospacedDigit())
                .frame(width: 42)
        }
    }

    private func slider(_ label: String, value: Binding<Double>,
                        in range: ClosedRange<Double>, unit: String,
                        onChange: @escaping (Double) -> Void) -> some View {
        HStack(spacing: 6) {
            Text(label).font(.caption)
            Slider(value: Binding(get: { value.wrappedValue },
                                  set: { value.wrappedValue = $0; onChange($0) }),
                   in: range)
            Text(String(format: "%.0f%@", value.wrappedValue, unit))
                .font(.caption.monospacedDigit())
        }
    }
}

// MARK: - Waveform editor with draggable trim/loop markers

struct WaveformEditor: View {
    @ObservedObject var model: EditorModel
    @State private var dragging: String? = nil

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let h = geo.size.height
            let frames = max(model.sampleFrames, 1)
            ZStack {
                // waveform
                Canvas { ctx, size in
                    let peaks = model.samplePeaks
                    guard !peaks.isEmpty else { return }
                    let mid = size.height / 2
                    var path = Path()
                    for (i, p) in peaks.enumerated() {
                        let x = CGFloat(i) / CGFloat(peaks.count) * size.width
                        let ph = max(1, CGFloat(p) * (size.height / 2 - 4))
                        path.move(to: CGPoint(x: x, y: mid - ph))
                        path.addLine(to: CGPoint(x: x, y: mid + ph))
                    }
                    ctx.stroke(path, with: .color(.accentColor.opacity(0.75)),
                               lineWidth: max(1, w / CGFloat(peaks.count)))
                }
                .background(Color.black.opacity(0.25))
                .clipShape(RoundedRectangle(cornerRadius: 8))

                // shaded trimmed-out regions
                let ts = x(of: "trim_start", w: w, frames: frames)
                let te = x(of: "trim_end", w: w, frames: frames, def: frames)
                Rectangle().fill(Color.black.opacity(0.45))
                    .frame(width: max(0, ts))
                    .position(x: max(0, ts) / 2, y: h / 2)
                Rectangle().fill(Color.black.opacity(0.45))
                    .frame(width: max(0, w - te))
                    .position(x: te + max(0, w - te) / 2, y: h / 2)

                marker("trim_start", color: .green, x: ts, h: h, label: "S")
                marker("trim_end", color: .green, x: te, h: h, label: "E")
                if (model.sampleInfo["no_loops"] as? Int ?? 0) > 0 {
                    marker("loop_start", color: .orange,
                           x: x(of: "loop_start", w: w, frames: frames), h: h, label: "L1")
                    marker("loop_end", color: .orange,
                           x: x(of: "loop_end", w: w, frames: frames, def: frames),
                           h: h, label: "L2")
                }
            }
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 2)
                .onChanged { g in
                    let frames = max(model.sampleFrames, 1)
                    if dragging == nil {
                        dragging = nearestMarker(to: g.startLocation.x, w: w,
                                                 frames: frames)
                    }
                    guard let param = dragging else { return }
                    let frame = Int((g.location.x / w).clamped01 * CGFloat(frames))
                    model.setSample(param, frame)
                }
                .onEnded { _ in dragging = nil })
        }
    }

    private func frameOf(_ key: String, def: Int = 0) -> Int {
        model.sampleInfo[key] as? Int ?? def
    }

    private func x(of key: String, w: CGFloat, frames: Int, def: Int = 0) -> CGFloat {
        CGFloat(frameOf(key, def: def)) / CGFloat(frames) * w
    }

    private func nearestMarker(to x: CGFloat, w: CGFloat, frames: Int) -> String {
        var candidates = ["trim_start": self.x(of: "trim_start", w: w, frames: frames),
                          "trim_end": self.x(of: "trim_end", w: w, frames: frames,
                                             def: frames)]
        if (model.sampleInfo["no_loops"] as? Int ?? 0) > 0 {
            candidates["loop_start"] = self.x(of: "loop_start", w: w, frames: frames)
            candidates["loop_end"] = self.x(of: "loop_end", w: w, frames: frames,
                                            def: frames)
        }
        return candidates.min { abs($0.value - x) < abs($1.value - x) }!.key
    }

    private func marker(_ key: String, color: Color, x: CGFloat, h: CGFloat,
                        label: String) -> some View {
        ZStack(alignment: .top) {
            Rectangle().fill(color).frame(width: 2, height: h)
            Text(label)
                .font(.system(size: 9, weight: .bold))
                .padding(.horizontal, 3)
                .background(color)
                .foregroundStyle(.black)
                .clipShape(RoundedRectangle(cornerRadius: 3))
        }
        .position(x: x, y: h / 2)
    }
}

extension CGFloat {
    var clamped01: CGFloat { Swift.min(1, Swift.max(0, self)) }
}
