# frozen_string_literal: true

require 'minitest/autorun'

# Ella Base v4.7 gate -> setting -> queue: source contract (no Flutter SDK). Behavioral twin:
# test/unit/on_device_base_gate_runner_test.dart (Flutter, SDK slot).
# Usage: ruby on_device_base_gate_source_contract_test.rb [ROOT of an overlaid tree]
class OnDeviceBaseGateSourceContractTest < Minitest::Test
  ROOT = File.expand_path(ARGV.first || File.join(__dir__, '..'))

  def src(path)
    File.read(File.join(ROOT, path), encoding: 'UTF-8')
  end

  def runner
    src('lib/services/on_device/on_device_runner.dart')
  end

  def run_body
    runner[/Future<void> run\(\{bool transcribeNow = false\}\) async \{(.*?)\n  Future<void> _process/m, 1] or
      flunk('run() not found')
  end

  def test_gate_opens_only_for_base
    gate = src('lib/services/experiments/on_device_overflow_gate.dart')
    assert_match(/\(profile \?\? Env\.profile\) == AppEnvironmentProfile\.ellaBase/, gate)
    assert_match(/catch \(_\) \{\n\s+return false;/, gate, 'other profiles stay fail-closed')
  end

  def test_opt_in_stays_disabled_by_default
    assert_match(/this\.enabled = false,/, src('lib/services/on_device/on_device_settings.dart'))
    body = run_body
    enabled = body.index('if (!controller.settings.enabled) return;') or flunk('enabled check')
    %w[rejectDisabledPreparedUpload controller.evaluate( markRun(running: true) _process(].each do |work|
      assert body.index(work) > enabled, "#{work} only after the opt-in check"
    end
  end

  def test_gate_alone_keeps_no_audio
    controller = src('lib/services/on_device/on_device_transcription_controller.dart')
    assert_match(/bool get shouldKeepAudioLocal => _active && _settings\.enabled &&/, controller)
    assert_match(/saveBool\('onDeviceOverflowConfigured', _active && _settings\.enabled\)/, controller)
    refute_match(/approvedWindow|OnDeviceRunScope/, controller, 'no scope subsystem')
  end

  def test_exact_selector_is_minimal_test_only_and_ephemeral
    assert_match(/@visibleForTesting\n  Set<String>\? debugOnlyWalFiles;/, runner)
    body = run_body
    enabled = body.index('if (!controller.settings.enabled) return;')
    filter = body.index('pending.retainWhere((w) => only.contains(w.filePath))') or flunk('filter')
    assert enabled < filter, 'selector applies only after the opt-in check'
    assert_match(/await _process\(wal, generation\);\n      \}\n      debugOnlyWalFiles = null;/, body, 'cleared per pass')
    refute File.exist?(File.join(ROOT, 'lib/services/on_device/on_device_run_scope.dart')), 'no scope subsystem'
  end

  def test_no_app_code_sets_the_selector
    users = Dir.glob(File.join(ROOT, 'lib/**/*.dart')).select do |path|
      File.read(path, encoding: 'UTF-8').include?('debugOnlyWalFiles')
    end
    assert_equal [File.join(ROOT, 'lib/services/on_device/on_device_runner.dart')], users
  end

  def test_unset_selector_leaves_the_queue_as_before
    body = run_body
    assert_match(/final only = debugOnlyWalFiles;\n\s+if \(only != null\) pending\.retainWhere/, body)
    assert_match(/for \(final wal in pending\) \{/, body, 'the loop still covers the whole opted-in queue')
  end
end
