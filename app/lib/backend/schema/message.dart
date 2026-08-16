import 'package:collection/collection.dart';
import 'package:uuid/uuid.dart';

enum MessageSender { ai, human }

enum MessageType {
  text('text'),
  daySummary('day_summary'),
  ;

  final String value;

  const MessageType(this.value);

  static MessageType valuesFromString(String value) {
    return MessageType.values.firstWhereOrNull((e) => e.value == value) ?? MessageType.text;
  }
}

class MessageConversationStructured {
  String title;
  String emoji;

  MessageConversationStructured(this.title, this.emoji);

  static MessageConversationStructured fromJson(Map<String, dynamic> json) {
    return MessageConversationStructured(json['title'], json['emoji']);
  }

  Map<String, dynamic> toJson() {
    return {
      'title': title,
      'emoji': emoji,
    };
  }
}

class MessageConversation {
  String id;
  DateTime createdAt;
  MessageConversationStructured structured;

  MessageConversation(this.id, this.createdAt, this.structured);

  static MessageConversation fromJson(Map<String, dynamic> json) {
    return MessageConversation(
      json['id'],
      DateTime.parse(json['created_at']).toLocal(),
      MessageConversationStructured.fromJson(json['structured']),
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'id': id,
      'created_at': createdAt.toUtc().toIso8601String(),
      'structured': structured.toJson(),
    };
  }
}

class MessageFile {
  String id;
  String openaiFileId;
  String? thumbnail;
  String? thumbnailName;
  String name;
  String mimeType;
  DateTime createdAt;

  MessageFile(this.openaiFileId, this.thumbnail, this.name, this.mimeType, this.id, this.createdAt, this.thumbnailName);

  static MessageFile fromJson(Map<String, dynamic> json) {
    return MessageFile(
      json['openai_file_id'],
      json['thumbnail'],
      json['name'],
      json['mime_type'],
      json['id'],
      DateTime.parse(json['created_at']).toLocal(),
      json['thumb_name'],
    );
  }

  static List<MessageFile> fromJsonList(List<dynamic> json) {
    return json.map((e) => MessageFile.fromJson(e)).toList();
  }

  Map<String, dynamic> toJson() {
    return {
      'openai_file_id': openaiFileId,
      'thumbnail': thumbnail,
      'name': name,
      'mime_type': mimeType,
      'id': id,
      'created_at': createdAt.toUtc().toIso8601String(),
      'thumb_name': thumbnailName,
    };
  }

  String mimeTypeToFileType() {
    if (mimeType.contains('image')) {
      return 'image';
    } else {
      return 'file';
    }
  }
}

class ChartDataPoint {
  String label;
  double value;

  ChartDataPoint(this.label, this.value);

  static ChartDataPoint fromJson(Map<String, dynamic> json) {
    return ChartDataPoint(
      json['label'] ?? '',
      (json['value'] as num).toDouble(),
    );
  }

  Map<String, dynamic> toJson() {
    return {'label': label, 'value': value};
  }
}

class ChartDataset {
  String label;
  List<ChartDataPoint> dataPoints;
  String? color;

  ChartDataset(this.label, this.dataPoints, {this.color});

  static ChartDataset fromJson(Map<String, dynamic> json) {
    return ChartDataset(
      json['label'] ?? 'Data',
      ((json['data_points'] ?? []) as List).map((p) => ChartDataPoint.fromJson(p)).toList(),
      color: json['color'],
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'label': label,
      'data_points': dataPoints.map((p) => p.toJson()).toList(),
      'color': color,
    };
  }
}

class ChartData {
  String chartType; // 'line' or 'bar'
  String title;
  String? xLabel;
  String? yLabel;
  List<ChartDataset> datasets;

  ChartData(this.chartType, this.title, this.datasets, {this.xLabel, this.yLabel});

  static ChartData? fromJson(Map<String, dynamic>? json) {
    if (json == null) return null;
    return ChartData(
      json['chart_type'] ?? 'line',
      json['title'] ?? '',
      ((json['datasets'] ?? []) as List).map((d) => ChartDataset.fromJson(d)).toList(),
      xLabel: json['x_label'],
      yLabel: json['y_label'],
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'chart_type': chartType,
      'title': title,
      'x_label': xLabel,
      'y_label': yLabel,
      'datasets': datasets.map((d) => d.toJson()).toList(),
    };
  }
}

class ServerMessage {
  String id;
  DateTime createdAt;
  String text;
  MessageSender sender;
  MessageType type;

  String? appId;
  bool fromIntegration;

  List<MessageFile> files;
  List filesId;

  List<MessageConversation> memories;
  bool askForNps;

  /// User rating for this message: 1 = thumbs up, -1 = thumbs down, null = no rating
  int? rating;

  /// Whether this message originated from voice chat (client-side only, not persisted)
  bool fromVoice;

  /// Durable canonical ordering fields. These survive cache round-trips so an
  /// equal-time user/assistant pair cannot be reordered when history is merged.
  String? canonicalConversationId;
  String? canonicalTurnId;
  int? canonicalEventSequence;

  List<String> thinkings = [];
  ChartData? chartData;

  ServerMessage(
    this.id,
    this.createdAt,
    this.text,
    this.sender,
    this.type,
    this.appId,
    this.fromIntegration,
    this.files,
    this.filesId,
    this.memories, {
    this.askForNps = true,
    this.rating,
    this.chartData,
    this.fromVoice = false,
    this.canonicalConversationId,
    this.canonicalTurnId,
    this.canonicalEventSequence,
  });

  static ServerMessage fromJson(Map<String, dynamic> json) {
    final metadata =
        json['metadata'] is Map ? Map<String, dynamic>.from(json['metadata'] as Map) : const <String, dynamic>{};
    return ServerMessage(
      json['id'],
      DateTime.parse(json['created_at']).toLocal(),
      json['text'] ?? "",
      MessageSender.values.firstWhere((e) => e.toString().split('.').last == json['sender']),
      MessageType.valuesFromString(json['type']),
      json['plugin_id'],
      json['from_integration'] ?? false,
      ((json['files'] ?? []) as List<dynamic>).map((m) => MessageFile.fromJson(m)).toList(),
      (json['files_id'] ?? []).map((m) => m.toString()).toList(),
      ((json['memories'] ?? []) as List<dynamic>).map((m) => MessageConversation.fromJson(m)).toList(),
      askForNps: json['ask_for_nps'] ?? true,
      rating: json['rating'],
      chartData: json['chart_data'] != null ? ChartData.fromJson(json['chart_data']) : null,
      fromVoice: json['from_voice'] ?? false,
      canonicalConversationId: metadata['conversation_id']?.toString(),
      canonicalTurnId: metadata['turn_id']?.toString(),
      canonicalEventSequence: metadata['event_sequence'] as int?,
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'id': id,
      'created_at': createdAt.toUtc().toIso8601String(),
      'text': text,
      'sender': sender.toString().split('.').last,
      'type': type.toString().split('.').last,
      'plugin_id': appId,
      'from_integration': fromIntegration,
      'memories': memories.map((m) => m.toJson()).toList(),
      'files': files.map((m) => m.toJson()).toList(),
      'ask_for_nps': askForNps,
      'rating': rating,
      'chart_data': chartData?.toJson(),
      'from_voice': fromVoice,
      if (canonicalConversationId != null || canonicalTurnId != null || canonicalEventSequence != null)
        'metadata': {
          if (canonicalConversationId != null) 'conversation_id': canonicalConversationId,
          if (canonicalTurnId != null) 'turn_id': canonicalTurnId,
          if (canonicalEventSequence != null) 'event_sequence': canonicalEventSequence,
        },
    };
  }

  String? get durableTurnId {
    final explicit = canonicalTurnId?.trim() ?? '';
    if (explicit.isNotEmpty) return explicit;
    for (final suffix in const [':user', ':assistant']) {
      if (id.endsWith(suffix) && id.length > suffix.length) return id.substring(0, id.length - suffix.length);
    }
    return null;
  }

  int? get durableEventSequence {
    if (canonicalEventSequence != null) return canonicalEventSequence;
    if (id.endsWith(':user')) return 0;
    if (id.endsWith(':assistant')) return 1;
    return null;
  }

  bool areFilesOfSameType() {
    if (files.isEmpty) {
      return true;
    }

    final firstType = files.first.mimeTypeToFileType();
    return files.every((element) => element.mimeTypeToFileType() == firstType);
  }

  static ServerMessage empty({String? appId}) {
    return ServerMessage(
      '0000',
      DateTime.now(),
      '',
      MessageSender.ai,
      MessageType.text,
      appId,
      false,
      [],
      [],
      [],
    );
  }

  static ServerMessage failedMessage() {
    return ServerMessage(
      const Uuid().v4(),
      DateTime.now(),
      'Looks like we are having issues with the server. Please try again later.',
      MessageSender.ai,
      MessageType.text,
      null,
      false,
      [],
      [],
      [],
    );
  }

  bool get isEmpty => id == '0000';
}

int compareServerMessagesChronologically(ServerMessage first, ServerMessage second) {
  final timestampOrder = first.createdAt.compareTo(second.createdAt);
  if (timestampOrder != 0) return timestampOrder;

  final firstTurnId = first.durableTurnId;
  final secondTurnId = second.durableTurnId;
  if (firstTurnId != null && secondTurnId != null) {
    final firstConversationId = first.canonicalConversationId?.trim() ?? '';
    final secondConversationId = second.canonicalConversationId?.trim() ?? '';
    if (firstConversationId.isNotEmpty && secondConversationId.isNotEmpty) {
      final conversationOrder = firstConversationId.compareTo(secondConversationId);
      if (conversationOrder != 0) return conversationOrder;
    }
    final turnOrder = firstTurnId.compareTo(secondTurnId);
    if (turnOrder != 0) return turnOrder;

    final sequenceOrder = (first.durableEventSequence ?? 2).compareTo(second.durableEventSequence ?? 2);
    if (sequenceOrder != 0) return sequenceOrder;
    final firstRoleOrder = first.sender == MessageSender.human ? 0 : 1;
    final secondRoleOrder = second.sender == MessageSender.human ? 0 : 1;
    final roleOrder = firstRoleOrder.compareTo(secondRoleOrder);
    if (roleOrder != 0) return roleOrder;
  }
  return first.id.compareTo(second.id);
}

enum MessageChunkType {
  think('think'),
  data('data'),
  done('done'),
  error('error'),
  message('message'),
  ;

  final String value;

  const MessageChunkType(this.value);
}

class ServerMessageChunk {
  String messageId;
  MessageChunkType type;
  String text;
  ServerMessage? message;

  ServerMessageChunk(
    this.messageId,
    this.text,
    this.type, {
    this.message,
  });

  static ServerMessageChunk failedMessage() {
    return ServerMessageChunk(
      const Uuid().v4(),
      'Looks like we are having issues with the server. Please try again later.',
      MessageChunkType.error,
    );
  }
}
