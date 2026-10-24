"""Exercise the production cold-scene adapter with inert UIKit/Flutter doubles.
No auth, network, capture or notification permission is exercised.
"""

import pathlib
import plistlib
import subprocess
import tempfile
import unittest

IOS = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (IOS / 'Runner/AppDelegate.swift').read_text()


class LifecycleRegressionTests(unittest.TestCase):
    def test_manifest_and_engine_boundaries(self):
        plist = plistlib.loads((IOS / 'Runner/Info.plist').read_bytes())
        manifest = plist['UIApplicationSceneManifest']
        self.assertFalse(manifest['UIApplicationSupportsMultipleScenes'])
        (config,) = manifest['UISceneConfigurations']['UIWindowSceneSessionRoleApplication']
        self.assertEqual(config['UISceneDelegateClassName'], '$(PRODUCT_MODULE_NAME).EllaSceneDelegate')
        self.assertEqual(config['UISceneStoryboardFile'], 'Main')
        self.assertNotIn('UIMainStoryboardFile', plist)
        launch, channels = SOURCE.split('func didInitializeImplicitFlutterEngine', 1)
        channels = channels.split('// MARK: - VoIP Push Registration', 1)[0]
        self.assertNotIn('window?.rootViewController', launch)
        self.assertNotIn('register(with: self)', launch)
        self.assertIn('register(with: engineBridge.pluginRegistry)', channels)
        self.assertIn('engineBridge.applicationRegistrar.messenger()', channels)
        for process_hook in [
            'clearScopedGuardianNotifications()',
            'requestNotificationAuthorization()',
            'registerForVoIPPushes()',
            'CallKitProvider.shared.setup()',
            'TwilioVoiceManager.shared.initialize()',
            'setPluginRegistrantCallback',
        ]:
            self.assertIn(process_hook, launch)
        for channel in [
            'com.friend.ios/notifyOnKill',
            'com.omi.apple_reminders',
            'com.omi.apple_health',
            'com.omi.ios/speech',
            'com.ellaaicare.ella/guardian_mode',
            'com.ellaaicare.ella/audio_route',
            'com.ellaaicare.ella/debug_events',
            'EllaUpstreamCaptureNativeHost.shared.register',
            'WatchRecorderHostAPISetup.setUp',
        ]:
            self.assertIn(channel, channels)

    def test_production_cold_scene_adapter(self):
        # Compile the exact production adapter body, not a second implementation.
        adapter = SOURCE[SOURCE.index('@objc class EllaSceneDelegate:') :].replace('@objc ', '', 1)
        stubs = r'''
import Foundation
var events: [String] = []
class UIScene {
  struct ConnectionOptions { var urlContexts: [UIOpenURLContext] = []; var userActivities: [NSUserActivity] = [] }
}
class UISceneSession {}
struct UIOpenURLContext {
  struct Options { var openInPlace = false; var sourceApplication: String?; var annotation: Any? }
  var url: URL; var options: Options
}
class UIApplication {
  static let shared = UIApplication()
  var delegate: AnyObject?
  struct OpenURLOptionsKey: Hashable {
    let value: String
    static let openInPlace = Self(value: "openInPlace")
    static let sourceApplication = Self(value: "sourceApplication")
    static let annotation = Self(value: "annotation")
  }
}
class FlutterSceneDelegate {
  func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options: UIScene.ConnectionOptions) {
    events.append("super")
  }
}
class AppDelegate {
  var received: [URL] = []
  var receivedOptions: [[UIApplication.OpenURLOptionsKey: Any]] = []
  var activities: [NSUserActivity] = []
  func application(_ app: UIApplication, open url: URL, options: [UIApplication.OpenURLOptionsKey: Any]) -> Bool {
    precondition(events.first == "super"); events.append("url"); received.append(url); receivedOptions.append(options); return false
  }
  func application(_ app: UIApplication, continue activity: NSUserActivity, restorationHandler: ([Any]?) -> Void) -> Bool {
    precondition(events.first == "super"); events.append("activity"); activities.append(activity); return true
  }
}
'''
        checks = r'''
let delegate = AppDelegate(); UIApplication.shared.delegate = delegate
let scene = EllaSceneDelegate()
scene.scene(UIScene(), willConnectTo: UISceneSession(), options: .init())
precondition(events == ["super"] && delegate.received.isEmpty)
events = []
let first = URL(string: "ella-test://first")!; let second = URL(string: "ella-test://second")!
let activity = NSUserActivity(activityType: "test")
scene.scene(UIScene(), willConnectTo: UISceneSession(), options: .init(urlContexts: [
  .init(url: first, options: .init(openInPlace: true, sourceApplication: "test.source", annotation: "token")),
  .init(url: second, options: .init())], userActivities: [activity]))
precondition(events == ["super", "url", "url", "activity"])
precondition(delegate.received == [first, second])
precondition(delegate.receivedOptions[0][.openInPlace] as? Bool == true)
precondition(delegate.receivedOptions[0][.sourceApplication] as? String == "test.source")
precondition(delegate.receivedOptions[0][.annotation] as? String == "token")
precondition(delegate.receivedOptions[1][.sourceApplication] == nil)
precondition(delegate.activities.count == 1 && delegate.activities[0] === activity)
events = []; UIApplication.shared.delegate = nil
scene.scene(UIScene(), willConnectTo: UISceneSession(), options: .init())
precondition(events == ["super"])
print("Production cold-scene adapter: empty launch, URL metadata, multiple URLs, activity, super ordering, missing delegate PASS")
'''
        with tempfile.TemporaryDirectory(prefix='iris-scene-regression-') as tmp:
            source = pathlib.Path(tmp) / 'main.swift'
            source.write_text(stubs + adapter + checks)
            executable = pathlib.Path(tmp) / 'tests'
            subprocess.run(
                [
                    'xcrun',
                    'swiftc',
                    '-module-cache-path',
                    str(pathlib.Path(tmp) / 'modules'),
                    str(source),
                    '-o',
                    str(executable),
                ],
                check=True,
            )
            subprocess.run([str(executable)], check=True)


if __name__ == '__main__':
    unittest.main()
