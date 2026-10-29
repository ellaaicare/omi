# frozen_string_literal: true

require 'minitest/autorun'
require 'open3'
require 'tmpdir'

# DEV-ONLY timed capture token gate through the PRODUCTION PhoneMicController (see the .swift harness).
# Same build shape as phone_mic_lifecycle_replay_test.rb, plus -D BASE_DEV_TIMED_CAPTURE, and the same
# drift-guarded Pigeon contract stub extraction. OMI_DEV_TEST_TMP pins the scratch root (default: system tmp).
class DevTimedCaptureControllerTest < Minitest::Test
  IOS_ROOT = File.expand_path('..', __dir__)

  def test_dev_token_gate_through_production_controller
    sources = %w[
      Runner/PhoneMic/PhoneMicController.swift
      Runner/PhoneMic/PhoneMicEventEmitter.swift
      Runner/PhoneMic/PhoneMicControllerSeams.swift
      Runner/PhoneMic/PhoneMicCaptureEngine.swift
      Runner/PhoneMic/PhoneMicConverterPipeline.swift
      test/dev_timed_capture_controller_test.swift
    ].map { |path| File.join(IOS_ROOT, path) }
    Dir.mktmpdir('omi-dev-token-gate', ENV['OMI_DEV_TEST_TMP']) do |directory|
      stub = File.join(directory, 'pigeon_stub.swift')
      File.write(stub, extract_pigeon_contract_stub)
      binary = File.join(directory, 'dev-token-gate-test')
      stdout, stderr, status = Open3.capture3('swiftc', '-parse-as-library', '-D', 'BASE_DEV_TIMED_CAPTURE',
                                              *sources, stub, '-o', binary)
      assert status.success?, "swiftc failed:\n#{stdout}\n#{stderr}"
      # Array form: the scratch root may contain spaces (OMI_DEV_TEST_TMP on an external volume).
      stdout, stderr, status = Open3.capture3([binary, binary])
      puts stdout
      assert status.success?, "dev token controller harness failed:\n#{stdout}\n#{stderr}"
      assert_match(/^PASS \d+ dev token controller checks$/, stdout)
    end
  end

  private

  def extract_pigeon_contract_stub
    generated = File.read(File.join(IOS_ROOT, 'Runner/PhoneMic/PhoneMicPigeon.g.swift'), encoding: 'UTF-8')
    blocks = [
      /enum PhoneMicCaptureState: Int \{.*?\n\}/m,
      /enum PhoneMicCaptureMode: Int \{.*?\n\}/m,
      /final class PhoneMicPigeonError: Error \{.*?\n\}/m
    ].map do |pattern|
      match = generated.match(pattern)
      refute_nil match, "Pigeon contract block #{pattern.source[0, 40]}... no longer matches PhoneMicPigeon.g.swift"
      match[0]
    end
    "import Foundation\n\n// Extracted from PhoneMicPigeon.g.swift at test time (drift-guarded).\n\n#{blocks.join("\n\n")}\n"
  end
end
