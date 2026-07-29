const bool isEllaInternalPilotEnabled = bool.fromEnvironment('ELLA_ENTITLEMENT_GATE', defaultValue: false);

bool isEllaInternalPilotLocaleSupported(String languageCode) => languageCode.trim().toLowerCase() == 'en';

bool canUseEllaInternalPilotLocale(
  String languageCode, {
  bool pilotEnabled = isEllaInternalPilotEnabled,
}) =>
    !pilotEnabled || isEllaInternalPilotLocaleSupported(languageCode);
