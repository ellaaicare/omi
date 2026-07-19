class EllaDisplayValue {
  final String text;
  final bool isEllaGenerated;

  const EllaDisplayValue({required this.text, required this.isEllaGenerated});
}

EllaDisplayValue parseEllaDisplayValue(String value) {
  final leadingWhitespace = RegExp(r'^\s*').firstMatch(value)?.group(0) ?? '';
  var content = value.substring(leadingWhitespace.length);
  var wing = '';
  if (content.startsWith('🪽')) {
    wing = '🪽 ';
    content = content.substring('🪽'.length).trimLeft();
  }
  final sourcePrefix = RegExp(r'^\[Ella\]\s*:?\s*', caseSensitive: false);
  final isEllaGenerated = sourcePrefix.hasMatch(content);
  content = content.replaceFirst(sourcePrefix, '');
  return EllaDisplayValue(
    text: '$leadingWhitespace$wing$content',
    isEllaGenerated: isEllaGenerated,
  );
}

String stripEllaDisplayPrefix(String value) => parseEllaDisplayValue(value).text;

String persistEllaDisplayValue(String value, {required bool isEllaGenerated}) {
  final displayValue = parseEllaDisplayValue(value);
  return isEllaGenerated ? '[Ella] ${displayValue.text.trimLeft()}' : displayValue.text;
}
