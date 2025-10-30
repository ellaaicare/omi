enum MemoryCategory { interesting, system }

enum MemoryVisibility { private, public }

class Memory {
  String id;
  String uid;
  String content;
  MemoryCategory category;
  List<String> tags;  // ADDED: Backend sends tags array
  DateTime createdAt;
  DateTime updatedAt;
  String? conversationId;
  bool reviewed;
  bool? userReview;
  bool manuallyAdded;
  bool edited;
  bool deleted;
  MemoryVisibility visibility;
  String? scoring;  // ADDED: Backend sends scoring field
  String? appId;  // ADDED: Backend sends app_id field
  String? dataProtectionLevel;  // ADDED: Backend sends data_protection_level field
  bool isLocked;

  Memory({
    required this.id,
    required this.uid,
    required this.content,
    required this.category,
    this.tags = const [],  // Default to empty list
    required this.createdAt,
    required this.updatedAt,
    this.conversationId,
    this.reviewed = false,
    this.userReview,
    this.manuallyAdded = false,
    this.edited = false,
    this.deleted = false,
    required this.visibility,
    this.scoring,  // Optional field
    this.appId,  // Optional field
    this.dataProtectionLevel,  // Optional field
    this.isLocked = false,
  });

  factory Memory.fromJson(Map<String, dynamic> json) {
    return Memory(
      id: json['id'],
      uid: json['uid'],
      content: json['content'],
      category: MemoryCategory.values.firstWhere(
        (e) => e.toString().split('.').last == json['category'],
        orElse: () => MemoryCategory.interesting,
      ),
      tags: json['tags'] != null ? List<String>.from(json['tags']) : [],  // ADDED: Parse tags array
      createdAt: DateTime.parse(json['created_at']).toLocal(),
      updatedAt: DateTime.parse(json['updated_at']).toLocal(),
      conversationId: json['conversation_id'],
      reviewed: json['reviewed'] ?? false,
      userReview: json['user_review'],
      manuallyAdded: json['manually_added'] ?? false,
      edited: json['edited'] ?? false,
      deleted: json['deleted'] ?? false,
      visibility: json['visibility'] != null
          ? (MemoryVisibility.values.asNameMap()[json['visibility']] ?? MemoryVisibility.public)
          : MemoryVisibility.public,
      scoring: json['scoring'],  // ADDED: Parse scoring field
      appId: json['app_id'],  // ADDED: Parse app_id field
      dataProtectionLevel: json['data_protection_level'],  // ADDED: Parse data_protection_level field
      isLocked: json['is_locked'] ?? false,
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'id': id,
      'uid': uid,
      'content': content,
      'category': category.toString().split('.').last,
      'tags': tags,  // ADDED: Include tags in JSON
      'created_at': createdAt.toUtc().toIso8601String(),
      'updated_at': updatedAt.toUtc().toIso8601String(),
      'memory_id': conversationId,
      'reviewed': reviewed,
      'user_review': userReview,
      'manually_added': manuallyAdded,
      'edited': edited,
      'deleted': deleted,
      'visibility': visibility,
      'scoring': scoring,  // ADDED: Include scoring in JSON
      'app_id': appId,  // ADDED: Include app_id in JSON
      'data_protection_level': dataProtectionLevel,  // ADDED: Include data_protection_level in JSON
      'is_locked': isLocked,
    };
  }
}
