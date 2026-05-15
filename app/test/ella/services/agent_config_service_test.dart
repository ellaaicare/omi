import 'package:flutter_test/flutter_test.dart';

import 'package:omi/ella/services/agent_config_service.dart';

class _FakeAgentConfigTransport implements AgentConfigTransport {
  final List<String> getPaths = [];
  final List<String> patchPaths = [];
  final List<Map<String, dynamic>> patchPayloads = [];
  AgentConfigResponse? getResponse;
  AgentConfigResponse? patchResponse;

  @override
  Future<AgentConfigResponse?> get(String path) async {
    getPaths.add(path);
    return getResponse;
  }

  @override
  Future<AgentConfigResponse?> patch(String path, Map<String, dynamic> payload) async {
    patchPaths.add(path);
    patchPayloads.add(payload);
    return patchResponse;
  }
}

void main() {
  group('AgentConfigService', () {
    late _FakeAgentConfigTransport transport;

    setUp(() {
      transport = _FakeAgentConfigTransport();
      AgentConfigService.transport = transport;
    });

    test('parses platform/provider/model and backend allowlists', () async {
      transport.getResponse = const AgentConfigResponse(
        statusCode: 200,
        body: {
          'platform': 'hermes',
          'provider': 'openai-codex',
          'model': 'gpt-5.5',
          'editable': {'platform': false, 'provider': true, 'model': true},
          'options': {
            'providers': ['openai-codex', 'anthropic'],
            'modelsByProvider': {
              'openai-codex': ['gpt-5.5', 'gpt-5.5-mini'],
              'anthropic': ['claude-opus-4-7'],
            },
          },
          'source': {'runtime': 'hermes', 'profile': 'plato', 'override': 'profile'},
        },
      );

      final config = await AgentConfigService.fetch();

      expect(transport.getPaths, [AgentConfigService.path]);
      expect(config, isNotNull);
      expect(config!.platform, 'hermes');
      expect(config.provider, 'openai-codex');
      expect(config.model, 'gpt-5.5');
      expect(config.editable.platform, isFalse);
      expect(config.editable.provider, isTrue);
      expect(config.options.providers, ['openai-codex', 'anthropic']);
      expect(config.options.modelsForProvider('openai-codex'), ['gpt-5.5', 'gpt-5.5-mini']);
      expect(config.source.runtime, 'hermes');
    });

    test('builds provider/model patch and preserves platform from previous config on empty success body', () async {
      const previous = AgentConfig(
        platform: 'hermes',
        provider: 'openai-codex',
        model: 'gpt-5.5',
        editable: AgentConfigEditable(platform: false, provider: true, model: true),
        options: AgentConfigOptions(
          providers: ['openai-codex'],
          modelsByProvider: {
            'openai-codex': ['gpt-5.5', 'gpt-5.5-mini'],
          },
        ),
        source: AgentConfigSource(runtime: 'hermes', profile: 'plato', override: 'profile'),
      );
      transport.patchResponse = const AgentConfigResponse(statusCode: 204, body: {});

      final updated = await AgentConfigService.update(
        provider: 'openai-codex',
        model: 'gpt-5.5-mini',
        previous: previous,
      );

      expect(transport.patchPaths, [AgentConfigService.path]);
      expect(transport.patchPayloads.single, {'provider': 'openai-codex', 'model': 'gpt-5.5-mini'});
      expect(updated, isNotNull);
      expect(updated!.platform, 'hermes');
      expect(updated.provider, 'openai-codex');
      expect(updated.model, 'gpt-5.5-mini');
    });
  });
}
