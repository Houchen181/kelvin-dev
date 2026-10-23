import AVFoundation
import KelvinKit
import Speech

/// Talk instead of type: speech recognized on the iPhone itself, into the message box. The audio never leaves the
/// phone. Voice only fills the box: it never sends, approves or changes anything.
@MainActor
final class Dictation: ObservableObject {
    @Published private(set) var listening = false
    @Published var problem: String?

    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var quiet: Task<Void, Never>?
    private var started = Date()
    private var run = 0                       // results from an earlier run are ignored

    static let firstWords: UInt64 = 6_000_000_000   // stop if nothing is said for this long
    static let pause: UInt64 = 2_500_000_000        // stop after this long a pause
    static let longest: TimeInterval = 60

    func toggle(typed: String, update: @escaping (String) -> Void) {
        if listening {
            stop()
        } else {
            Task { await start(typed: typed, update: update) }
        }
    }

    func start(typed: String, update: @escaping (String) -> Void) async {
        problem = nil
        guard !listening, task == nil else { return }
        guard await Dictation.allowed() else {
            problem = "Kelvin may not use the microphone or speech recognition. Allow both in Settings → Kelvin."
            return
        }
        guard let recognizer = SFSpeechRecognizer(locale: Locale.current) ?? SFSpeechRecognizer(locale: Locale(identifier: "en-US")),
              recognizer.isAvailable else {
            problem = "Speech recognition isn't available right now."
            return
        }
        guard recognizer.supportsOnDeviceRecognition else {
            let language = Locale.current.localizedString(forIdentifier: recognizer.locale.identifier) ?? "this language"
            problem = "This iPhone can't recognize \(language) on the phone itself, and Kelvin doesn't send your voice "
                + "anywhere. Use the keyboard's microphone instead."
            return
        }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            problem = "There is no microphone here."
            return
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.addsPunctuation = true
        request.taskHint = .dictation
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.record, mode: .measurement, options: .duckOthers)
            try session.setActive(true, options: .notifyOthersOnDeactivation)
            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: format, block: Dictation.feed(request))
            engine.prepare()
            try engine.start()
        } catch {
            problem = "The microphone didn't start: \(error.localizedDescription)"
            finish()
            return
        }
        run += 1
        let mine = run
        self.request = request
        listening = true
        started = Date()
        wait(Dictation.firstWords)
        task = Dictation.listen(recognizer, request) { [weak self] heard, final, failed in
            Task { @MainActor in
                guard let self, mine == self.run else { return }
                if let heard { update(Voice.merge(typed, heard)) }
                if final || failed {
                    self.finish()
                } else if self.listening {
                    self.wait(Dictation.pause)
                }
            }
        }
    }

    /// Stop listening; the last words still arrive, then it tidies up.
    func stop() {
        guard listening else { return }
        listening = false
        quiet?.cancel()
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        let mine = run
        Task { @MainActor [weak self] in       // the final result normally comes at once; don't wait for ever
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            if let self, mine == self.run, self.task != nil { self.finish() }
        }
    }

    /// Stop at once and drop whatever is still being recognized (the message is being sent, or the chat closed).
    func cancel() {
        guard listening || task != nil else { return }
        finish()
    }

    private func finish() {
        run += 1
        quiet?.cancel()
        quiet = nil
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        request = nil
        task?.cancel()
        task = nil
        listening = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func wait(_ nanoseconds: UInt64) {
        quiet?.cancel()
        if Date().timeIntervalSince(started) > Dictation.longest {
            stop()
            return
        }
        quiet = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: nanoseconds)
            guard !Task.isCancelled, let self else { return }
            self.stop()
        }
    }

    static func allowed() async -> Bool {
        let speech = await withCheckedContinuation { (c: CheckedContinuation<SFSpeechRecognizerAuthorizationStatus, Never>) in
            SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0) }
        }
        guard speech == .authorized else { return false }
        if #available(iOS 17, *) {
            return await AVAudioApplication.requestRecordPermission()
        }
        return await withCheckedContinuation { (c: CheckedContinuation<Bool, Never>) in
            AVAudioSession.sharedInstance().requestRecordPermission { c.resume(returning: $0) }
        }
    }

    // Made outside the main actor: the audio and the recognizer call these on their own threads.

    nonisolated private static func feed(_ request: SFSpeechAudioBufferRecognitionRequest) -> AVAudioNodeTapBlock {
        { buffer, _ in request.append(buffer) }
    }

    nonisolated private static func listen(_ recognizer: SFSpeechRecognizer, _ request: SFSpeechAudioBufferRecognitionRequest,
                                           _ heard: @escaping (String?, Bool, Bool) -> Void) -> SFSpeechRecognitionTask {
        recognizer.recognitionTask(with: request) { result, error in
            heard(result?.bestTranscription.formattedString, result?.isFinal ?? false, error != nil)
        }
    }
}
