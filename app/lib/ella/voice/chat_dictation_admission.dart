/// Chat's new dictation control must not enter the existing shared recorder
/// until phone capture, native capture and realtime calls share an exact owner
/// reservation that spans permission, start, stop and account transitions.
///
/// This admission does not request permission, access a service, or mutate any
/// queued recordings. There is deliberately no ready/success outcome yet.
enum ChatDictationAdmissionResult { exclusiveOwnershipUnavailable }

class ChatDictationAdmission {
  const ChatDictationAdmission();

  bool get isAvailable => false;

  ChatDictationAdmissionResult requestStart() => ChatDictationAdmissionResult.exclusiveOwnershipUnavailable;
}
