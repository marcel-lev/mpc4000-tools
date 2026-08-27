import SwiftUI
import AppKit
import UniformTypeIdentifiers

@main
struct Exs2AkpApp: App {
    var body: some Scene {
        WindowGroup("MPC EXS to MPC") {
            ContentView()
        }
    }
}

struct FileItem: Identifiable, Equatable {
    let id = UUID()
    let url: URL
}

final class ConverterModel: ObservableObject {
    @Published var files: [FileItem] = []
    @Published var outputDir: URL
    @Published var running = false
    @Published var succeeded: Bool? = nil
    @Published var log = ""

    private var process: Process?

    init() {
        if let saved = UserDefaults.standard.string(forKey: "outputDir") {
            outputDir = URL(fileURLWithPath: saved)
        } else {
            let downloads = FileManager.default.urls(for: .downloadsDirectory,
                                                     in: .userDomainMask).first
                ?? FileManager.default.homeDirectoryForCurrentUser
            outputDir = downloads.appendingPathComponent("AKP Programs")
        }
    }

    func setOutputDir(_ url: URL) {
        outputDir = url
        UserDefaults.standard.set(url.path, forKey: "outputDir")
    }

    func addURLs(_ urls: [URL]) {
        var found: [URL] = []
        let fm = FileManager.default
        for url in urls {
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: url.path, isDirectory: &isDir) else { continue }
            if isDir.boolValue {
                if let en = fm.enumerator(at: url, includingPropertiesForKeys: nil) {
                    for case let f as URL in en where f.pathExtension.lowercased() == "exs" {
                        found.append(f)
                        if found.count > 500 { break }
                    }
                }
            } else if url.pathExtension.lowercased() == "exs" {
                found.append(url)
            }
        }
        let existing = Set(files.map { $0.url.path })
        for f in found where !existing.contains(f.path) {
            files.append(FileItem(url: f))
        }
    }

    func convert() {
        guard !running, !files.isEmpty else { return }
        guard let script = Bundle.main.url(forResource: "exs2akp", withExtension: "py") else {
            log = "ERROR: bundled exs2akp.py not found\n"
            succeeded = false
            return
        }
        try? FileManager.default.createDirectory(at: outputDir,
                                                 withIntermediateDirectories: true)
        running = true
        succeeded = nil
        log = ""

        var args = [script.path, "convert"]
        args += files.map { $0.url.path }
        args += ["-o", outputDir.path]

        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.log += text }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                pipe.fileHandleForReading.readabilityHandler = nil
                if let rest = try? pipe.fileHandleForReading.readToEnd(),
                   let text = String(data: rest, encoding: .utf8) {
                    self?.log += text
                }
                self?.running = false
                self?.succeeded = proc.terminationStatus == 0
                    && !(self?.log.contains("FAILED") ?? false)
            }
        }
        process = p
        do {
            try p.run()
        } catch {
            running = false
            succeeded = false
            log += "ERROR: could not run python3: \(error.localizedDescription)\n"
        }
    }

    func revealOutput() {
        NSWorkspace.shared.activateFileViewerSelecting([outputDir])
    }
}

struct ContentView: View {
    @StateObject private var model = ConverterModel()
    @State private var dropTargeted = false

    var body: some View {
        VStack(spacing: 14) {
            dropZone
            if !model.files.isEmpty { fileList }
            outputRow
            controls
            if !model.log.isEmpty { logView }
        }
        .padding(20)
        .frame(minWidth: 620, minHeight: model.log.isEmpty ? 420 : 560)
    }

    private var dropZone: some View {
        VStack(spacing: 10) {
            Image(systemName: "arrow.down.document")
                .font(.system(size: 40, weight: .light))
                .foregroundStyle(dropTargeted ? Color.accentColor : .secondary)
            Text("Drop EXS24 instruments here")
                .font(.title3)
            Text("…or click to choose files (folders are scanned for .exs)")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .frame(height: 160)
        .background(
            RoundedRectangle(cornerRadius: 14)
                .fill(dropTargeted ? Color.accentColor.opacity(0.12)
                                   : Color.primary.opacity(0.04))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .strokeBorder(dropTargeted ? Color.accentColor : Color.secondary.opacity(0.4),
                              style: StrokeStyle(lineWidth: 1.5, dash: [7, 5]))
        )
        .contentShape(RoundedRectangle(cornerRadius: 14))
        .onTapGesture { chooseFiles() }
        .onDrop(of: [UTType.fileURL], isTargeted: $dropTargeted) { providers in
            for provider in providers {
                provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier,
                                  options: nil) { item, _ in
                    var url: URL?
                    if let data = item as? Data {
                        url = URL(dataRepresentation: data, relativeTo: nil)
                    } else if let u = item as? URL {
                        url = u
                    }
                    if let u = url {
                        DispatchQueue.main.async { model.addURLs([u]) }
                    }
                }
            }
            return true
        }
    }

    private var fileList: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("\(model.files.count) instrument\(model.files.count == 1 ? "" : "s")")
                    .font(.headline)
                Spacer()
                Button("Clear") { model.files.removeAll() }
                    .buttonStyle(.link)
                    .disabled(model.running)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(model.files) { item in
                        HStack(spacing: 6) {
                            Image(systemName: "waveform")
                                .foregroundStyle(.secondary)
                                .font(.caption)
                            Text(item.url.deletingPathExtension().lastPathComponent)
                            Text(item.url.deletingLastPathComponent().path
                                    .replacingOccurrences(
                                        of: FileManager.default.homeDirectoryForCurrentUser.path,
                                        with: "~"))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer()
                            Button {
                                model.files.removeAll { $0 == item }
                            } label: {
                                Image(systemName: "xmark.circle.fill")
                                    .foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                            .disabled(model.running)
                        }
                        .padding(.vertical, 1)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 130)
        }
    }

    private var outputRow: some View {
        HStack(spacing: 8) {
            Image(systemName: "folder")
                .foregroundStyle(.secondary)
            Text("Output:")
            Text(model.outputDir.path
                    .replacingOccurrences(
                        of: FileManager.default.homeDirectoryForCurrentUser.path,
                        with: "~"))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer()
            Button("Choose…") { chooseOutputDir() }
                .disabled(model.running)
        }
    }

    private var controls: some View {
        HStack {
            Text("Output: 24-bit / 44.1 kHz WAV")
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer()
            if model.running {
                ProgressView()
                    .controlSize(.small)
                    .padding(.trailing, 4)
            }
            if model.succeeded == true {
                Button("Show in Finder") { model.revealOutput() }
            }
            Button(model.running ? "Converting…" : "Convert") { model.convert() }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(model.running || model.files.isEmpty)
        }
    }

    private var logView: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                if let ok = model.succeeded {
                    Image(systemName: ok ? "checkmark.circle.fill"
                                         : "exclamationmark.triangle.fill")
                        .foregroundStyle(ok ? .green : .orange)
                    Text(ok ? "Done — programs and samples written."
                            : "Finished with problems — see log.")
                        .font(.headline)
                }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    Text(model.log)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Color.clear.frame(height: 1).id("end")
                }
                .frame(minHeight: 100, maxHeight: .infinity)
                .background(Color.primary.opacity(0.04),
                            in: RoundedRectangle(cornerRadius: 8))
                .onChange(of: model.log) { _ in
                    proxy.scrollTo("end", anchor: .bottom)
                }
            }
        }
    }

    private func chooseFiles() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        if let exs = UTType(filenameExtension: "exs") {
            panel.allowedContentTypes = [exs]
        }
        panel.message = "Choose EXS24 instruments (or folders to scan)"
        if panel.runModal() == .OK {
            model.addURLs(panel.urls)
        }
    }

    private func chooseOutputDir() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.prompt = "Choose"
        panel.message = "Choose the output folder for .akp programs and samples"
        panel.directoryURL = model.outputDir
        if panel.runModal() == .OK, let url = panel.urls.first {
            model.setOutputDir(url)
        }
    }
}
