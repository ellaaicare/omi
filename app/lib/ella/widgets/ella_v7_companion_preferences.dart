import 'dart:math' as math;
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:omi/backend/preferences.dart';

/// Local decoration only. Neither capture status nor Ella's account identity is renamed.
class EllaV7CompanionPreferences extends ChangeNotifier {
  EllaV7CompanionPreferences() {
    SharedPreferencesUtil.aiConsentAuthorityChanges.addListener(() => Future.microtask(notifyListeners));
  }
  static final shared = EllaV7CompanionPreferences();
  String _key(String field, String uid) => 'ellaV7Companion.$uid.$field';
  Map<String, dynamic> _read() {
    try {
      final decoded = jsonDecode(SharedPreferencesUtil().getString(_key('style', SharedPreferencesUtil().uid)));
      if (decoded is Map<String, dynamic>) return decoded;
    } catch (_) {}
    return const {};
  }

  String get name => _read()['name'] is String ? _read()['name'] as String : '';
  int get colorIndex {
    final stored = _read()['color'];
    return stored is int ? stored.clamp(0, 2) : 0;
  }

  int get styleIndex {
    final stored = _read()['shape'];
    return stored is int ? stored.clamp(0, 2) : 0;
  }

  Future<bool> save({required String uid, required String name, required int colorIndex, int? styleIndex}) async {
    final p = SharedPreferencesUtil();
    if (uid.isEmpty ||
        p.uid != uid ||
        colorIndex < 0 ||
        colorIndex > 2 ||
        (styleIndex != null && (styleIndex < 0 || styleIndex > 2))) return false;
    if (!await p.saveString(_key('style', uid),
        jsonEncode({'name': name.trim(), 'color': colorIndex, 'shape': styleIndex ?? this.styleIndex}))) return false;
    if (p.uid != uid) return false;
    notifyListeners();
    return true;
  }
}

/// The three v7 board swatches: original, hue150, hue260/saturation0.8.
List<double> ellaV7LumeColorMatrix(int index) {
  final angle = (index == 1
          ? 150
          : index == 2
              ? 260
              : 0) *
      math.pi /
      180;
  final c = math.cos(angle), s = math.sin(angle), saturation = index == 2 ? .8 : 1.0;
  final hue = [
    .213 + c * .787 - s * .213,
    .715 - c * .715 - s * .715,
    .072 - c * .072 + s * .928,
    .213 - c * .213 + s * .143,
    .715 + c * .285 + s * .140,
    .072 - c * .072 - s * .283,
    .213 - c * .213 - s * .787,
    .715 - c * .715 + s * .715,
    .072 + c * .928 + s * .072,
  ];
  final weights = [.213, .715, .072];
  return [
    for (var row = 0; row < 3; row++) ...[
      for (var col = 0; col < 3; col++) weights[col] * (1 - saturation) + hue[row * 3 + col] * saturation,
      0,
      0,
    ],
    0,
    0,
    0,
    1,
    0,
  ];
}
