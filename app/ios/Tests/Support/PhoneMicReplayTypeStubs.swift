import Foundation
// Generated-channel and converter mechanism types only; controller/seams/emitter
// under test are the exact production files, not reproduced state machines.
enum PhoneMicCaptureMode { case stream, batch }
enum PhoneMicCaptureState { case idle, starting, running, interrupted, rebuilding }
struct PhoneMicPigeonError: Error { let code: String; let message: String; let details: Any? }
enum PhoneMicCaptureEngine { enum EngineError: Error { case formatInvalid, converterInitFailed } }
enum PhoneMicConverterPipeline { enum ConvertError { case formatDrift, allocationFailed, converter } }
