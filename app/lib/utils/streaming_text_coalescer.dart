class StreamingTextCoalescer {
  final Map<String, String> _textByUtterance = {};

  String addPartial(String utteranceId, String partial) {
    final current = _textByUtterance[utteranceId] ?? '';
    final next = mergeStreamingText(current, partial);
    _textByUtterance[utteranceId] = next;
    return next;
  }

  void complete(String utteranceId) => _textByUtterance.remove(utteranceId);
}

String mergeStreamingText(String current, String incoming) {
  if (incoming.isEmpty) return current;
  if (current.isEmpty || incoming.startsWith(current)) return incoming;
  if (current.startsWith(incoming) || current.endsWith(incoming)) return current;

  final maxOverlap = current.length < incoming.length ? current.length : incoming.length;
  for (var overlap = maxOverlap; overlap > 0; overlap--) {
    if (current.endsWith(incoming.substring(0, overlap))) {
      return current + incoming.substring(overlap);
    }
  }
  return current + incoming;
}
