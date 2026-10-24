/// Presentation-only evidence of append-only words in one exact live session.
class CaptureWordReceipt {
  String? _session;
  List<({String id, String text})> _words = [];
  DateTime? _arrival;
  void observe({required String? session, required List<({String id, String text})> words, required DateTime now}) {
    if (session == null || session != _session) {
      _session = session;
      _words = List.of(words);
      _arrival = null;
      return;
    }
    final prefixUnchanged = words.length >= _words.length &&
        List.generate(_words.length, (i) => i).every((i) =>
            words[i].id == _words[i].id &&
            (i == _words.length - 1 ? words[i].text.startsWith(_words[i].text) : words[i].text == _words[i].text));
    final added = words.skip(_words.length).any((w) => w.text.trim().isNotEmpty) ||
        (words.isNotEmpty &&
            _words.isNotEmpty &&
            words.length >= _words.length &&
            words[_words.length - 1].text.length > _words.last.text.length &&
            words[_words.length - 1].text.substring(_words.last.text.length).trim().isNotEmpty);
    if (!prefixUnchanged || !words.any((w) => w.text.trim().isNotEmpty)) _arrival = null;
    if (prefixUnchanged && added && words.any((w) => w.text.trim().isNotEmpty)) _arrival = now;
    _words = List.of(words);
  }

  Duration? age(DateTime now, {required String? session}) =>
      session != null && session == _session && _arrival != null && !now.isBefore(_arrival!)
          ? now.difference(_arrival!)
          : null;

  bool recent(DateTime now, {required String? session}) =>
      session != null &&
      session == _session &&
      _arrival != null &&
      !now.isBefore(_arrival!) &&
      now.difference(_arrival!) < const Duration(seconds: 6);
}
