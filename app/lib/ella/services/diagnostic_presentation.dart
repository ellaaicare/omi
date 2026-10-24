import 'package:intl/intl.dart';

/// Local time is readable and has an explicit UTC offset; UTC remains copyable.
String diagnosticTimestamp(DateTime value, String locale) {
  final local = value.toLocal();
  final offset = local.timeZoneOffset;
  final minutes = offset.inMinutes.abs();
  final hours = (minutes ~/ 60).toString().padLeft(2, '0');
  final remainder = (minutes % 60).toString().padLeft(2, '0');
  final sign = offset.isNegative ? '-' : '+';
  return '${DateFormat.yMMMd(locale).add_Hms().format(local)} ${local.timeZoneName} (UTC$sign$hours:$remainder)\n'
      'raw_utc=${value.toUtc().toIso8601String()}';
}

/// Clipboard output is deliberately narrower than the existing owner readout.
Map<String, Object> supportVoiceFields(Map<String, Object> fields) => {
      for (final key in const [
        'http_status',
        'voice_mode',
        'one_shot_tts_provider',
        'one_shot_tts_candidates',
        'provider_type',
        'fallback_used',
        'app_build',
        'platform',
      ])
        if (fields.containsKey(key)) key: fields[key]!,
    };
