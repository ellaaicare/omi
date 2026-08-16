const String generatedImageAssetContractVersion = 'ella.generated_image.asset.v1';
const String generatedImageDeliveryPrefix = '/v1/ella/generated-image-assets/';

/// Canonical-confirmed reference to a privately delivered generated image.
///
/// Parsing is intentionally fail closed. Callers must preserve their source
/// photo or static artwork fallback whenever this returns null.
class GeneratedImageAsset {
  const GeneratedImageAsset({
    required this.assetId,
    required this.jobId,
    required this.receiptId,
    required this.generation,
    required this.mediaType,
    required this.sha256,
    required this.width,
    required this.height,
    required this.altText,
    required this.deliveryPath,
  });

  static final RegExp _digestPattern = RegExp(r'^sha256:[0-9a-f]{64}$');
  static const Set<String> _supportedMediaTypes = {'image/jpeg', 'image/png', 'image/webp'};

  final String assetId;
  final String jobId;
  final String receiptId;
  final int generation;
  final String mediaType;
  final String sha256;
  final int width;
  final int height;
  final String altText;
  final String deliveryPath;

  static GeneratedImageAsset? tryFromJson(Object? value) {
    if (value is! Map) return null;
    final contractVersion = value['contract_version']?.toString().trim() ?? '';
    final assetId = value['asset_id']?.toString().trim() ?? '';
    final jobId = value['job_id']?.toString().trim() ?? '';
    final receiptId = value['receipt_id']?.toString().trim() ?? '';
    final generationValue = value['generation'];
    final mediaType = value['media_type']?.toString().trim() ?? '';
    final digest = value['sha256']?.toString().trim() ?? '';
    final widthValue = value['width'];
    final heightValue = value['height'];
    final moderationStatus = value['moderation_status']?.toString().trim() ?? '';
    final altText = value['alt_text']?.toString().trim() ?? '';
    final deliveryPath = value['delivery_path']?.toString().trim() ?? '';
    final pathIsSafe = deliveryPath == '$generatedImageDeliveryPrefix$assetId' &&
        !deliveryPath.endsWith('/') &&
        !deliveryPath.contains('?') &&
        !deliveryPath.contains('#') &&
        !deliveryPath.contains('..');

    if (contractVersion != generatedImageAssetContractVersion ||
        assetId.isEmpty ||
        jobId.isEmpty ||
        receiptId.isEmpty ||
        generationValue is! num ||
        generationValue != generationValue.toInt() ||
        generationValue.toInt() < 1 ||
        !_supportedMediaTypes.contains(mediaType) ||
        !_digestPattern.hasMatch(digest) ||
        widthValue is! num ||
        widthValue != widthValue.toInt() ||
        widthValue.toInt() < 1 ||
        widthValue.toInt() > 16384 ||
        heightValue is! num ||
        heightValue != heightValue.toInt() ||
        heightValue.toInt() < 1 ||
        heightValue.toInt() > 16384 ||
        moderationStatus != 'approved' ||
        altText.isEmpty ||
        altText.length > 500 ||
        !pathIsSafe) {
      return null;
    }

    return GeneratedImageAsset(
      assetId: assetId,
      jobId: jobId,
      receiptId: receiptId,
      generation: generationValue.toInt(),
      mediaType: mediaType,
      sha256: digest,
      width: widthValue.toInt(),
      height: heightValue.toInt(),
      altText: altText,
      deliveryPath: deliveryPath,
    );
  }

  Map<String, dynamic> toJson() => {
        'contract_version': generatedImageAssetContractVersion,
        'asset_id': assetId,
        'job_id': jobId,
        'receipt_id': receiptId,
        'generation': generation,
        'media_type': mediaType,
        'sha256': sha256,
        'width': width,
        'height': height,
        'moderation_status': 'approved',
        'alt_text': altText,
        'delivery_path': deliveryPath,
      };
}
