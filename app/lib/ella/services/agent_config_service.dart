import 'dart:convert';

import 'package:flutter/foundation.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';

class AgentConfigResponse {
  const AgentConfigResponse({required this.statusCode, required this.body});

  final int statusCode;
  final Map<String, dynamic> body;

  bool get isSuccess => statusCode >= 200 && statusCode < 300;
}

abstract class AgentConfigTransport {
  Future<AgentConfigResponse?> get(String path);

  Future<AgentConfigResponse?> patch(String path, Map<String, dynamic> payload);
}

class _AgentConfigHttpTransport implements AgentConfigTransport {
  @override
  Future<AgentConfigResponse?> get(String path) async {
    final response = await makeApiCall(
      url: '${Env.apiBaseUrl}$path',
      headers: const {'Content-Type': 'application/json'},
      method: 'GET',
      body: '',
      timeout: const Duration(seconds: 10),
      retries: 0,
    );
    if (response == null) return null;
    return AgentConfigResponse(statusCode: response.statusCode, body: _decodeBody(response.body));
  }

  @override
  Future<AgentConfigResponse?> patch(String path, Map<String, dynamic> payload) async {
    final response = await makeApiCall(
      url: '${Env.apiBaseUrl}$path',
      headers: const {'Content-Type': 'application/json'},
      method: 'PATCH',
      body: jsonEncode(payload),
      timeout: const Duration(seconds: 10),
      retries: 0,
    );
    if (response == null) return null;
    return AgentConfigResponse(statusCode: response.statusCode, body: _decodeBody(response.body));
  }

  static Map<String, dynamic> _decodeBody(String raw) {
    if (raw.trim().isEmpty) return {};
    try {
      final decoded = jsonDecode(raw);
      if (decoded is Map<String, dynamic>) return decoded;
    } catch (_) {}
    return {};
  }
}

class AgentConfig {
  const AgentConfig({
    required this.platform,
    required this.provider,
    required this.model,
    required this.editable,
    required this.options,
    required this.source,
  });

  final String platform;
  final String provider;
  final String model;
  final AgentConfigEditable editable;
  final AgentConfigOptions options;
  final AgentConfigSource source;

  factory AgentConfig.fromJson(Map<String, dynamic> json) {
    return AgentConfig(
      platform: _readString(json['platform']),
      provider: _readString(json['provider']),
      model: _readString(json['model']),
      editable: AgentConfigEditable.fromJson(_readMap(json['editable'])),
      options: AgentConfigOptions.fromJson(_readMap(json['options'])),
      source: AgentConfigSource.fromJson(_readMap(json['source'])),
    );
  }

  AgentConfig copyWith({
    String? platform,
    String? provider,
    String? model,
    AgentConfigEditable? editable,
    AgentConfigOptions? options,
    AgentConfigSource? source,
  }) {
    return AgentConfig(
      platform: platform ?? this.platform,
      provider: provider ?? this.provider,
      model: model ?? this.model,
      editable: editable ?? this.editable,
      options: options ?? this.options,
      source: source ?? this.source,
    );
  }
}

class AgentConfigEditable {
  const AgentConfigEditable({
    required this.platform,
    required this.provider,
    required this.model,
  });

  final bool platform;
  final bool provider;
  final bool model;

  factory AgentConfigEditable.fromJson(Map<String, dynamic> json) {
    return AgentConfigEditable(
      platform: json['platform'] == true,
      provider: json['provider'] == true,
      model: json['model'] == true,
    );
  }
}

class AgentConfigOptions {
  const AgentConfigOptions({
    required this.providers,
    required this.modelsByProvider,
  });

  final List<String> providers;
  final Map<String, List<String>> modelsByProvider;

  factory AgentConfigOptions.fromJson(Map<String, dynamic> json) {
    final providers = _readStringList(json['providers']);
    final modelsByProvider = <String, List<String>>{};
    final rawModelsByProvider = json['modelsByProvider'];

    if (rawModelsByProvider is Map<String, dynamic>) {
      for (final entry in rawModelsByProvider.entries) {
        modelsByProvider[entry.key] = _readStringList(entry.value);
      }
    }

    return AgentConfigOptions(
      providers: providers,
      modelsByProvider: modelsByProvider,
    );
  }

  List<String> modelsForProvider(String provider) => modelsByProvider[provider] ?? const [];
}

class AgentConfigSource {
  const AgentConfigSource({
    required this.runtime,
    required this.profile,
    required this.override,
  });

  final String runtime;
  final String profile;
  final String override;

  factory AgentConfigSource.fromJson(Map<String, dynamic> json) {
    return AgentConfigSource(
      runtime: _readString(json['runtime']),
      profile: _readString(json['profile']),
      override: _readString(json['override']),
    );
  }
}

class AgentConfigService {
  AgentConfigService._();

  static const path = 'v1/ella/agent-config';

  @visibleForTesting
  static AgentConfigTransport transport = _AgentConfigHttpTransport();

  static Future<AgentConfig?> fetch() async {
    final response = await transport.get(path);
    if (response == null || !response.isSuccess) return null;
    return AgentConfig.fromJson(response.body);
  }

  static Future<AgentConfig?> update({
    required String provider,
    required String model,
    AgentConfig? previous,
  }) async {
    final response = await transport.patch(path, buildPatchPayload(provider: provider, model: model));
    if (response == null || !response.isSuccess) return null;
    if (response.body.isNotEmpty) return AgentConfig.fromJson(response.body);
    return previous?.copyWith(provider: provider, model: model);
  }

  @visibleForTesting
  static Map<String, dynamic> buildPatchPayload({
    required String provider,
    required String model,
  }) {
    return {
      'provider': provider,
      'model': model,
    };
  }
}

String _readString(dynamic value) {
  if (value is String) return value.trim();
  return '';
}

Map<String, dynamic> _readMap(dynamic value) {
  if (value is Map<String, dynamic>) return value;
  return {};
}

List<String> _readStringList(dynamic value) {
  if (value is! List) return const [];
  return value.whereType<String>().map((item) => item.trim()).where((item) => item.isNotEmpty).toList(growable: false);
}
