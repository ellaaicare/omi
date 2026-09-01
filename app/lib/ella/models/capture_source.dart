enum EllaCaptureSource {
  phone,
  necklace;

  static EllaCaptureSource? fromStorage(String value) => switch (value.trim()) {
        'phone' => EllaCaptureSource.phone,
        'necklace' => EllaCaptureSource.necklace,
        _ => null,
      };
}
