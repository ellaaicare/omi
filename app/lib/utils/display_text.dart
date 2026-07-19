String stripEllaDisplayPrefix(String value) {
  final leadingWhitespace = RegExp(r'^\s*').firstMatch(value)?.group(0) ?? '';
  var content = value.substring(leadingWhitespace.length);
  var wing = '';
  if (content.startsWith('🪽')) {
    wing = '🪽 ';
    content = content.substring('🪽'.length).trimLeft();
  }
  content = content.replaceFirst(RegExp(r'^\[Ella\]\s*:?\s*', caseSensitive: false), '');
  return '$leadingWhitespace$wing$content';
}
