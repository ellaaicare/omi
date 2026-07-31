import 'package:flutter/foundation.dart';

const String todayCardContractVersion = 'ella.today_card.v1';

enum TodayCardStatus {
  ready,
  preparing,
  newUser,
  degraded;

  static TodayCardStatus? tryParse(String value) => switch (value) {
        'ready' => TodayCardStatus.ready,
        'preparing' => TodayCardStatus.preparing,
        'new_user' => TodayCardStatus.newUser,
        'degraded' => TodayCardStatus.degraded,
        _ => null,
      };

  String get wireValue => switch (this) {
        TodayCardStatus.newUser => 'new_user',
        _ => name,
      };
}

enum TodayCardKind {
  recap,
  memory,
  interest,
  welcome;

  static TodayCardKind? tryParse(String value) => switch (value) {
        'recap' => TodayCardKind.recap,
        'memory' => TodayCardKind.memory,
        'interest' => TodayCardKind.interest,
        'welcome' => TodayCardKind.welcome,
        _ => null,
      };
}

@immutable
class TodayCardSourceRef {
  const TodayCardSourceRef({required this.kind, required this.id, this.versionId = ''});

  final String kind;
  final String id;
  final String versionId;

  Map<String, dynamic> toCacheJson() => {'kind': kind, 'id': id, if (versionId.isNotEmpty) 'version_id': versionId};

  static TodayCardSourceRef? fromCacheJson(Object? value) {
    if (value is! Map) return null;
    final kind = value['kind']?.toString().trim() ?? '';
    final id = value['id']?.toString().trim() ?? '';
    if (kind.isEmpty || id.isEmpty) return null;
    return TodayCardSourceRef(kind: kind, id: id, versionId: value['version_id']?.toString().trim() ?? '');
  }
}

@immutable
class TodayCard {
  const TodayCard({
    required this.id,
    required this.version,
    required this.kind,
    required this.eyebrow,
    required this.headline,
    required this.body,
    required this.generatedAt,
    this.spokenText = '',
    this.sourceDate = '',
    this.sourceRefs = const [],
  });

  final String id;
  final int version;
  final TodayCardKind kind;
  final String eyebrow;
  final String headline;
  final String body;
  final String spokenText;
  final String sourceDate;
  final DateTime generatedAt;
  final List<TodayCardSourceRef> sourceRefs;

  bool get isValid =>
      id.trim().isNotEmpty &&
      version > 0 &&
      eyebrow.trim().isNotEmpty &&
      headline.trim().isNotEmpty &&
      body.trim().isNotEmpty &&
      (kind == TodayCardKind.welcome || sourceRefs.isNotEmpty);

  String get textForSpeech => spokenText.trim().isNotEmpty ? spokenText.trim() : body.trim();

  Map<String, dynamic> toCacheJson() => {
        'id': id,
        'version': version,
        'kind': kind.name,
        'eyebrow': eyebrow,
        'headline': headline,
        'body': body,
        'spoken_text': spokenText,
        'source_date': sourceDate,
        'generated_at': generatedAt.toUtc().toIso8601String(),
        'source_refs': sourceRefs.map((source) => source.toCacheJson()).toList(),
      };

  static TodayCard? fromCacheJson(Object? value) {
    if (value is! Map) return null;
    final kind = TodayCardKind.tryParse(value['kind']?.toString() ?? '');
    final generatedAt = DateTime.tryParse(value['generated_at']?.toString() ?? '');
    final rawVersion = value['version'];
    final sourceRefs = (value['source_refs'] as List?)
            ?.map(TodayCardSourceRef.fromCacheJson)
            .whereType<TodayCardSourceRef>()
            .toList(growable: false) ??
        const <TodayCardSourceRef>[];
    if (kind == null || generatedAt == null || rawVersion is! num) return null;

    final card = TodayCard(
      id: value['id']?.toString().trim() ?? '',
      version: rawVersion.toInt(),
      kind: kind,
      eyebrow: value['eyebrow']?.toString().trim() ?? '',
      headline: value['headline']?.toString().trim() ?? '',
      body: value['body']?.toString().trim() ?? '',
      spokenText: value['spoken_text']?.toString().trim() ?? '',
      sourceDate: value['source_date']?.toString().trim() ?? '',
      generatedAt: generatedAt,
      sourceRefs: sourceRefs,
    );
    return card.isValid ? card : null;
  }
}

@immutable
class TodayCardResponse {
  const TodayCardResponse({
    required this.contractVersion,
    required this.status,
    this.card,
    this.errorCode = '',
    this.retryable = true,
  });

  final String contractVersion;
  final TodayCardStatus status;
  final TodayCard? card;
  final String errorCode;
  final bool retryable;

  bool get hasCurrentContract => contractVersion == todayCardContractVersion;

  bool get isValid {
    if (!hasCurrentContract) return false;
    if (status == TodayCardStatus.ready) return card?.isValid == true;
    if (status == TodayCardStatus.newUser && card != null) return card!.isValid && card!.kind == TodayCardKind.welcome;
    return card == null || card!.isValid;
  }
}

@immutable
class TodayCardViewState {
  const TodayCardViewState({
    required this.status,
    this.card,
    this.isLoading = false,
    this.isCached = false,
    this.errorCode = '',
  });

  const TodayCardViewState.preparing({this.card, this.isCached = false, this.isLoading = true})
      : status = TodayCardStatus.preparing,
        errorCode = '';

  final TodayCardStatus status;
  final TodayCard? card;
  final bool isLoading;
  final bool isCached;
  final String errorCode;
}
