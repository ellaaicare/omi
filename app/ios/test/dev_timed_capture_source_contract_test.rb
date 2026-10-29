# frozen_string_literal: true

require 'minitest/autorun'

# DEV-ONLY timed capture (v4.7) source contract. Reads source only: no Swift toolchain, Xcode, simulator or device.
# Guards the v4.6 token 461300147 failure: the dev AudioRecordingIntent could not start its Live Activity from the
# background, perform() returned with an owned recording and no card, and the app quit with no stop evidence.
# ROOT: an overlaid tree (default: this tree). Usage: ruby dev_timed_capture_source_contract_test.rb [ROOT]
class DevTimedCaptureSourceContractTest < Minitest::Test
  ROOT = File.expand_path(ARGV.first || File.join(__dir__, '..', '..'))
  SIRI = File.read(File.join(ROOT, 'ios/Runner/SiriIntegration/SiriIntents.swift'), encoding: 'UTF-8')
  LAM = File.read(File.join(ROOT, 'ios/Runner/LiveActivity/LiveActivityManager.swift'), encoding: 'UTF-8')

  def run_body
    SIRI[/static func run\(\) async throws -> String \{(.*?)\n    \}\n/m, 1] or flunk('run() not found')
  end

  def test_dev_intent_can_start_live_activity_from_background
    assert_match(/struct BaseTimedCaptureTestIntent: AudioRecordingIntent, LiveActivityIntent \{/, SIRI)
  end

  def test_dev_intent_matches_production_recording_intent_shape
    production = File.read(File.join(ROOT, 'ios/LiveActivity/OmiCaptureIntents.swift'), encoding: 'UTF-8')
    assert_match(/struct OmiCaptureRecordingIntent: AudioRecordingIntent, LiveActivityIntent/, production)
    assert_match(/static var openAppWhenRun: Bool = false/, SIRI[/struct BaseTimedCaptureTestIntent.*?\n\}/m])
  end

  def test_missing_card_cuts_the_owned_recording_before_perform_returns
    body = run_body
    policy = body.index('BaseTimedCaptureCardPolicy.afterStart') or flunk('policy not consulted')
    cut = body.index('cutoffOffMain(token') or flunk('no owned cutoff')
    close = body.rindex('setLiveActivityWindow(nil)')
    outcome = body.rindex('"native_trigger_outcome"')
    assert policy < cut, 'cutoff follows the policy'
    assert cut < close && close < outcome, 'cut before the window closes and before the outcome line'
    assert_match(/outcome = "cut_\\\(reason\):\\\(result\)"/, body)
  end

  def test_cutoff_never_runs_on_main_or_the_intent_task
    helper = SIRI[/private static func cutoffOffMain.*?\n    \}\n/m] or flunk('cutoffOffMain missing')
    assert_match(/DispatchQueue\.global\(\)\.async/, helper)
    refute_match(/MainActor|DispatchQueue\.main/, helper)
  end

  def test_live_token_lifecycle
    assert_match(/Self\.liveToken = token\n\s+DispatchQueue\.global\(\)\.asyncAfter/, SIRI, 'set on grant')
    assert_equal 2, SIRI.scan('if Self.liveToken == token { Self.liveToken = nil }').size, 'cleared on revoke and expiry'
  end

  def test_refused_card_reason_reaches_evidence
    assert_match(/static var devLastStartError: String\?/, LAM)
    assert_match(/if Self\.startAllowedOutsideForeground \{ Self\.devLastStartError = String\(describing: error\) \}/, LAM)
    assert_match(/"start_error": \$0/, run_body)
  end

  def test_policy_block_is_extractable_and_dev_only
    block = SIRI[%r{^// BEGIN DEV CARD POLICY$(.*?)^// END DEV CARD POLICY$}m, 1] or flunk('policy block missing')
    assert_match(/enum BaseTimedCaptureCardPolicy/, block)
    refute_match(/import|Flutter|Activity</, block, 'pure: no frameworks')
    dev_start = SIRI.index('#if BASE_DEV_TIMED_CAPTURE')
    assert dev_start && dev_start < SIRI.index('// BEGIN DEV CARD POLICY'), 'compiled only in dev builds'
  end
end
