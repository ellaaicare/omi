import 'package:flutter/services.dart';

Future<void> fonts() async {
  for (final entry in {
    'Manrope': 'assets/fonts/Manrope-400.ttf',
    'Fraunces': 'assets/fonts/Fraunces-Latin-Regular.ttf',
    'MaterialIcons': 'fonts/MaterialIcons-Regular.otf',
  }.entries) {
    final loader = FontLoader(entry.key)..addFont(rootBundle.load(entry.value));
    await loader.load();
  }
}
