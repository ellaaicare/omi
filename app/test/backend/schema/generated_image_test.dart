import 'package:flutter_test/flutter_test.dart';

import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/backend/schema/generated_image.dart';
import 'package:omi/backend/schema/memory.dart';

void main() {
  const digest = 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

  Map<String, dynamic> assetJson() => {
        'contract_version': generatedImageAssetContractVersion,
        'asset_id': 'asset-fixture',
        'job_id': 'job-fixture',
        'receipt_id': 'receipt-fixture',
        'generation': 3,
        'media_type': 'image/webp',
        'sha256': digest,
        'width': 1024,
        'height': 768,
        'moderation_status': 'approved',
        'alt_text': 'A gentle watercolor scene inspired by the memory.',
        'delivery_path': '${generatedImageDeliveryPrefix}asset-fixture',
      };

  Map<String, dynamic> conversationJson(Object? generatedImage) => {
        'id': 'conversation-fixture',
        'created_at': '2026-08-10T12:00:00Z',
        'structured': {
          'title': 'A grounded memory',
          'overview': 'A source-backed summary.',
          'emoji': '',
          'category': 'other',
          'action_items': [],
          'events': [],
        },
        'generated_image': generatedImage,
      };

  Map<String, dynamic> memoryJson(Object? generatedImage) => {
        'id': 'memory-fixture',
        'uid': 'owner-fixture',
        'content': 'A source-backed memory.',
        'category': 'interesting',
        'created_at': '2026-08-10T12:00:00Z',
        'updated_at': '2026-08-10T12:00:00Z',
        'visibility': 'private',
        'generated_image': generatedImage,
      };

  test('parses a canonical-confirmed first-party asset on both memory schemas', () {
    final conversation = ServerConversation.fromJson(conversationJson(assetJson()));
    final memory = Memory.fromJson(memoryJson(assetJson()));

    expect(conversation.generatedImage?.assetId, 'asset-fixture');
    expect(memory.generatedImage?.assetId, 'asset-fixture');
    expect(conversation.toJson()['generated_image'], assetJson());
    expect(memory.toJson()['generated_image'], assetJson());
  });

  test('falls back safely for absent, malformed, unmoderated, or externally delivered images', () {
    final invalidAssets = <Object?>[
      null,
      'not-a-map',
      {...assetJson(), 'moderation_status': 'pending'},
      {...assetJson(), 'contract_version': 'ella.generated_image.asset.v2'},
      {...assetJson(), 'delivery_path': 'https://provider.example/private-output.webp'},
      {...assetJson(), 'delivery_path': '$generatedImageDeliveryPrefix../other-owner'},
      {...assetJson(), 'delivery_path': '${generatedImageDeliveryPrefix}other-asset'},
      {...assetJson(), 'sha256': 'sha256:short'},
      {...assetJson(), 'generation': 1.5},
      {...assetJson(), 'width': 1.5},
    ];

    for (final invalidAsset in invalidAssets) {
      expect(ServerConversation.fromJson(conversationJson(invalidAsset)).generatedImage, isNull);
      expect(Memory.fromJson(memoryJson(invalidAsset)).generatedImage, isNull);
    }
  });
}
