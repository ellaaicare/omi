import Foundation
import CallKit
import AVFoundation

class CallKitProvider: NSObject {
    static let shared = CallKitProvider()

    private let provider: CXProvider
    private let callController = CXCallController()

    private override init() {
        // Configure provider
        let configuration = CXProviderConfiguration(localizedName: "Ella AI")
        configuration.supportsVideo = false
        configuration.maximumCallsPerCallGroup = 1
        configuration.supportedHandleTypes = [.generic]

        provider = CXProvider(configuration: configuration)

        super.init()

        provider.setDelegate(self, queue: nil)
    }

    func setup() {
        // Placeholder for initialization
        print("CallKitProvider: setup called")
    }
}

// MARK: - CXProviderDelegate
extension CallKitProvider: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        print("CallKitProvider: provider did reset")
    }
}
