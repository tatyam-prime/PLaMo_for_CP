import Foundation

/// Keep native stderr and Python tracebacks bounded even during a long app session.
final class BoundedLog {
    private let url: URL
    private let maxBytes: Int
    private let backups: Int
    private let lock = NSLock()
    private var handle: FileHandle?
    private var size = 0

    init(url: URL, maxBytes: Int = 128 * 1024, backups: Int = 1) throws {
        precondition(maxBytes > 0 && backups >= 0)
        self.url = url
        self.maxBytes = maxBytes
        self.backups = backups
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let prefix = url.lastPathComponent + "."
        for file in try FileManager.default.contentsOfDirectory(at: url.deletingLastPathComponent(), includingPropertiesForKeys: nil) {
            if file.lastPathComponent.hasPrefix(prefix),
               let index = Int(file.lastPathComponent.dropFirst(prefix.count)), index > backups {
                try FileManager.default.removeItem(at: file)
            }
        }
        for index in 0...backups {
            let file = index == 0 ? url : backup(index)
            if FileManager.default.fileExists(atPath: file.path),
               let count = (try FileManager.default.attributesOfItem(atPath: file.path)[.size] as? NSNumber)?.intValue,
               count > maxBytes {
                let source = try FileHandle(forReadingFrom: file)
                try source.seek(toOffset: UInt64(count - maxBytes))
                let tail = try source.read(upToCount: maxBytes) ?? Data()
                try source.close()
                try tail.write(to: file)
            }
        }
        try? FileManager.default.removeItem(at: url.appendingPathExtension("previous"))
        try openCurrent()
    }

    private func backup(_ index: Int) -> URL { url.appendingPathExtension(String(index)) }

    private func openCurrent() throws {
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        handle = try FileHandle(forWritingTo: url)
        size = Int(try handle!.seekToEnd())
    }

    private func rotate() throws {
        try handle?.close()
        handle = nil
        if backups > 0 {
            try? FileManager.default.removeItem(at: backup(backups))
            if backups > 1 {
                for index in stride(from: backups - 1, through: 1, by: -1) {
                    let previous = backup(index)
                    if FileManager.default.fileExists(atPath: previous.path) {
                        try FileManager.default.moveItem(at: previous, to: backup(index + 1))
                    }
                }
            }
            try FileManager.default.moveItem(at: url, to: backup(1))
        } else {
            try FileManager.default.removeItem(at: url)
        }
        try openCurrent()
    }

    func append(_ data: Data) throws {
        lock.lock()
        defer { lock.unlock() }
        guard handle != nil else { return }
        var offset = 0
        while offset < data.count {
            if size >= maxBytes { try rotate() }
            let end = min(data.count, offset + maxBytes - size)
            try handle!.write(contentsOf: data.subdata(in: offset..<end))
            size += end - offset
            offset = end
        }
    }

    func close() {
        lock.lock()
        defer { lock.unlock() }
        try? handle?.close()
        handle = nil
    }

    deinit { close() }
}
