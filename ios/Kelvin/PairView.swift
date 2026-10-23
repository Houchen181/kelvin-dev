import AVFoundation
import KelvinKit
import SwiftUI
import UIKit

/// Pair with a Kelvin computer: scan the QR code it shows, or type its address and the 6-digit code.
struct PairView: View {
    enum Mode: String, CaseIterable { case scan = "Scan the code", type = "Type it" }

    @EnvironmentObject var app: AppModel
    @Environment(\.dismiss) var dismiss
    @State var mode: Mode = .scan
    @State var address = ""
    @State var code = ""
    @State var working = false
    @State var failure: String?
    @State var lastScan: String?
    @State var retry: PairingLink?        // a scanned code whose computer didn't answer

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("How", selection: $mode) {
                        ForEach(Mode.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.segmented)
                } footer: {
                    Text("On the Kelvin computer, open Settings → Phone, switch on Phone access, name this phone, "
                         + "choose what it may do, and press Pair a phone.")
                }
                if mode == .scan {
                    Section {
                        ScannerView { found($0) }
                            .frame(height: 320)
                            .listRowInsets(EdgeInsets())
                    } footer: {
                        Text("Point the camera at the QR code. Away from the lab, pair with the code made for the "
                             + "Tailscale address.")
                    }
                } else {
                    Section {
                        TextField("Address, e.g. 192.168.1.20", text: $address)
                            .keyboardType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        TextField("6-digit code", text: $code)
                            .keyboardType(.numberPad)
                            .textContentType(.oneTimeCode)
                    } footer: {
                        Text("The address and code Kelvin shows under Pair a phone. The code works once, for 10 minutes.")
                    }
                    Section {
                        Button("Pair") { Task { await pair(typed: address, code: code) } }
                            .disabled(working || address.isEmpty || !Pairing.isCode(Pairing.digits(code)))
                    }
                }
                if working {
                    Section { HStack(spacing: 10) { ProgressView(); Text("Pairing…") } }
                }
                if let failure {
                    Section {
                        Text(failure).foregroundStyle(.red)
                        if let link = retry, let code = link.code, !working {
                            Button("Try again") { Task { await pair(addresses: link.addresses, code: code) } }
                        }
                    }
                }
            }
            .navigationTitle("Pair with Kelvin")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
        }
    }

    /// A QR code came into view. The same code is tried once by itself (a wrong one must not count against pairing
    /// again); when the computer didn't answer, Try again tries it once more.
    @MainActor
    func found(_ text: String) {
        guard !working, text != lastScan else { return }
        lastScan = text
        guard let link = Pairing.parse(text) else {
            failure = "That QR code is not a Kelvin pairing code."
            return
        }
        guard let code = link.code else {
            address = link.base.absoluteString
            mode = .type
            failure = "That code holds Kelvin's address but no pairing code: type the 6 digits."
            return
        }
        Task { await pair(addresses: link.addresses, code: code, link: link) }
    }

    @MainActor
    func pair(typed: String, code: String) async {
        guard let base = Pairing.address(typed) else {
            failure = "That is not an address. Type it as Kelvin shows it, e.g. 192.168.1.20:8771."
            return
        }
        await pair(addresses: [base], code: code)
    }

    @MainActor
    func pair(addresses: [URL], code: String, link: PairingLink? = nil) async {
        working = true
        failure = nil
        defer { working = false }
        do {
            try await app.pair(at: addresses, code: code)
            dismiss()
        } catch let e as KelvinError where e.kind == .unreachable {
            if let link { retry = link }
            failure = e.message + " " + PairView.unreachable
        } catch {
            retry = nil
            failure = error.localizedDescription
        }
    }

    static let unreachable = "Is this phone on the same Wi-Fi as the computer, with phone access on there? On Windows, "
        + "press Let phones through in Kelvin's Settings → Phone. If iOS asked to find devices on your local network, "
        + "allow it (Settings → Kelvin → Local Network). Many university, housing and hotel Wi-Fi networks keep devices "
        + "apart: then install Tailscale on both and pair with the code Kelvin shows."
}

/// The camera, reporting each QR code it sees.
struct ScannerView: UIViewControllerRepresentable {
    var found: (String) -> Void

    func makeUIViewController(context: Context) -> ScannerController {
        let c = ScannerController()
        c.found = found
        return c
    }

    func updateUIViewController(_ controller: ScannerController, context: Context) {
        controller.found = found
    }
}

/// Hands the camera's QR codes on (on the main queue, where the output delivers them).
final class QRRelay: NSObject, AVCaptureMetadataOutputObjectsDelegate {
    var found: ((String) -> Void)?

    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput objects: [AVMetadataObject],
                        from connection: AVCaptureConnection) {
        for case let code as AVMetadataMachineReadableCodeObject in objects {
            if let text = code.stringValue { found?(text) }
        }
    }
}

final class ScannerController: UIViewController {
    var found: ((String) -> Void)?
    private let session = AVCaptureSession()
    private let relay = QRRelay()
    private var preview: AVCaptureVideoPreviewLayer?
    private let message = UILabel()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        message.textColor = .white
        message.numberOfLines = 0
        message.textAlignment = .center
        message.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(message)
        NSLayoutConstraint.activate([
            message.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            message.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 24),
            message.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -24),
        ])
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            configure()
        case .notDetermined:
            Task {
                if await AVCaptureDevice.requestAccess(for: .video) { configure() } else { say(ScannerController.denied) }
            }
        default:
            say(ScannerController.denied)
        }
    }

    static let denied = "Kelvin may not use the camera. Allow it in Settings → Kelvin, or type the address and code."

    private func configure() {
        guard let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else {
            say("There is no camera here. Type the address and code instead.")
            return
        }
        session.addInput(input)
        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else { return }
        session.addOutput(output)
        relay.found = { [weak self] text in self?.found?(text) }
        output.setMetadataObjectsDelegate(relay, queue: .main)
        output.metadataObjectTypes = [.qr]
        let layer = AVCaptureVideoPreviewLayer(session: session)
        layer.videoGravity = .resizeAspectFill
        layer.frame = view.layer.bounds
        view.layer.insertSublayer(layer, at: 0)
        preview = layer
        let camera = session
        DispatchQueue.global(qos: .userInitiated).async { camera.startRunning() }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        preview?.frame = view.layer.bounds
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        let camera = session
        DispatchQueue.global(qos: .userInitiated).async { if camera.isRunning { camera.stopRunning() } }
    }

    private func say(_ text: String) {
        message.text = text
    }
}
