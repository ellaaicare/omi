import 'package:flutter/material.dart';

import 'package:font_awesome_flutter/font_awesome_flutter.dart';
import 'package:provider/provider.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/home_provider.dart';
import 'package:omi/providers/locale_provider.dart';
import 'package:omi/providers/user_provider.dart';
import 'package:omi/utils/analytics/mixpanel.dart';
import 'package:omi/utils/l10n_extensions.dart';

class LanguageSettingsPage extends StatefulWidget {
  const LanguageSettingsPage({super.key});

  @override
  State<LanguageSettingsPage> createState() => _LanguageSettingsPageState();
}

class _LanguageSettingsPageState extends State<LanguageSettingsPage> {
  bool _isUpdatingLanguage = false;

  Widget _buildSectionHeader(String title) {
    return Padding(
      padding: const EdgeInsets.only(left: 4, bottom: 8),
      child: Text(
        title,
        style: TextStyle(
          color: Colors.grey.shade500,
          fontSize: 13,
          fontWeight: FontWeight.w600,
          letterSpacing: 0.5,
        ),
      ),
    );
  }

  Widget _buildAppInterfaceCard(LocaleProvider localeProvider) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: EllaColors.bgTertiary),
      ),
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTap: () => _showAppLanguageSelectionSheet(localeProvider),
        child: Row(
          children: [
            Container(
              width: 40,
              height: 40,
              decoration: BoxDecoration(
                color: EllaColors.bgTertiary,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Center(
                child: FaIcon(
                  FontAwesomeIcons.textHeight,
                  color: EllaColors.primary,
                  size: 16,
                ),
              ),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    context.l10n.appLanguage,
                    style: const TextStyle(
                      color: EllaColors.textPrimary,
                      fontSize: 16,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    localeProvider.locale != null
                        ? LocaleProvider.getDisplayName(localeProvider.locale!)
                        : context.l10n.systemDefault,
                    style: TextStyle(
                      color: EllaColors.textSecondary,
                      fontSize: 13,
                    ),
                  ),
                ],
              ),
            ),
            FaIcon(
              FontAwesomeIcons.chevronRight,
              color: EllaColors.textTertiary,
              size: 14,
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildSpeechTranscriptionCard(
    HomeProvider homeProvider,
    UserProvider userProvider,
    CaptureProvider captureProvider,
  ) {
    final languageName = homeProvider.userPrimaryLanguage.isNotEmpty
        ? homeProvider.availableLanguages.entries
            .firstWhere(
              (element) => element.value == homeProvider.userPrimaryLanguage,
              orElse: () => MapEntry(context.l10n.notSet, ''),
            )
            .key
        : context.l10n.notSet;

    final isUpdatingTranslation = userProvider.isUpdatingSingleLanguageMode;
    final isAutoTranslationEnabled = !userProvider.singleLanguageMode;

    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: EllaColors.bgTertiary),
      ),
      child: Column(
        children: [
          // Speech Language Row
          GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTap: _isUpdatingLanguage ? null : () => _showLanguageSelectionSheet(homeProvider, captureProvider),
            child: Row(
              children: [
                Container(
                  width: 40,
                  height: 40,
                  decoration: BoxDecoration(
                    color: EllaColors.bgTertiary,
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Center(
                    child: FaIcon(
                      FontAwesomeIcons.microphone,
                      color: EllaColors.primary,
                      size: 16,
                    ),
                  ),
                ),
                const SizedBox(width: 14),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        context.l10n.primaryLanguage,
                        style: const TextStyle(
                          color: EllaColors.textPrimary,
                          fontSize: 16,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        languageName,
                        style: TextStyle(
                          color: EllaColors.textSecondary,
                          fontSize: 13,
                        ),
                      ),
                    ],
                  ),
                ),
                if (_isUpdatingLanguage)
                  const SizedBox(
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      valueColor: AlwaysStoppedAnimation<Color>(EllaColors.primary),
                    ),
                  )
                else
                  FaIcon(
                    FontAwesomeIcons.chevronRight,
                    color: EllaColors.textTertiary,
                    size: 14,
                  ),
              ],
            ),
          ),

          Padding(
            padding: const EdgeInsets.symmetric(vertical: 16),
            child: Divider(height: 1, color: EllaColors.bgTertiary),
          ),

          // Multi-language Detection Row
          Row(
            children: [
              Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  color: EllaColors.bgTertiary,
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Center(
                  child: FaIcon(
                    FontAwesomeIcons.language,
                    color: EllaColors.primary,
                    size: 16,
                  ),
                ),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      context.l10n.automaticTranslation,
                      style: const TextStyle(
                        color: EllaColors.textPrimary,
                        fontSize: 16,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      context.l10n.detectLanguages,
                      style: const TextStyle(
                        color: EllaColors.textSecondary,
                        fontSize: 13,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 12),
              if (isUpdatingTranslation)
                const SizedBox(
                  width: 24,
                  height: 24,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    valueColor: AlwaysStoppedAnimation<Color>(EllaColors.primary),
                  ),
                )
              else
                Switch(
                  value: isAutoTranslationEnabled,
                  onChanged: (value) async {
                    final success = await userProvider.setSingleLanguageMode(!value);
                    if (success && context.mounted) {
                      context.read<CaptureProvider>().onTranscriptionSettingsChanged();
                    }
                  },
                  activeThumbColor: EllaColors.primary,
                  activeTrackColor: EllaColors.primarySubtle,
                ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildHelperText() {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
      child: Text(
        context.l10n.languageSettingsHelperText,
        style: const TextStyle(
          color: EllaColors.textTertiary,
          fontSize: 12,
          height: 1.4,
        ),
      ),
    );
  }

  void _showAppLanguageSelectionSheet(LocaleProvider localeProvider) {
    final supportedLocales = LocaleProvider.supportedLocales;
    final currentLocale = localeProvider.locale;

    showModalBottomSheet(
      context: context,
      backgroundColor: Colors.white,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (context) {
        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                margin: const EdgeInsets.only(top: 12, bottom: 16),
                width: 36,
                height: 4,
                decoration: BoxDecoration(
                  color: EllaColors.bgTertiary,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              Text(
                context.l10n.appLanguage,
                style: const TextStyle(
                  color: EllaColors.textPrimary,
                  fontSize: 17,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 16),
              Flexible(
                child: ListView.builder(
                  shrinkWrap: true,
                  physics: const ClampingScrollPhysics(),
                  itemCount: supportedLocales.length,
                  itemBuilder: (context, index) {
                    final locale = supportedLocales[index];
                    final isSelected = currentLocale?.languageCode == locale.languageCode;
                    return ListTile(
                      title: Text(
                        LocaleProvider.getDisplayName(locale),
                        style: TextStyle(
                          color: isSelected ? EllaColors.primaryDark : EllaColors.textSecondary,
                          fontWeight: isSelected ? FontWeight.w500 : FontWeight.w400,
                        ),
                      ),
                      trailing: isSelected ? const Icon(Icons.check, color: EllaColors.primary, size: 20) : null,
                      onTap: () {
                        localeProvider.setLocale(locale);
                        Navigator.pop(context);
                      },
                    );
                  },
                ),
              ),
              const SizedBox(height: 16),
            ],
          ),
        );
      },
    );
  }

  void _showLanguageSelectionSheet(HomeProvider homeProvider, CaptureProvider captureProvider) {
    final languages = homeProvider.availableLanguages;
    String currentLanguage = homeProvider.userPrimaryLanguage;

    showModalBottomSheet(
      context: context,
      backgroundColor: Colors.white,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      isScrollControlled: true,
      builder: (sheetContext) {
        return StatefulBuilder(
          builder: (context, setSheetState) {
            return DraggableScrollableSheet(
              initialChildSize: 0.7,
              minChildSize: 0.5,
              maxChildSize: 0.9,
              expand: false,
              builder: (context, scrollController) {
                return Column(
                  children: [
                    Container(
                      margin: const EdgeInsets.only(top: 12, bottom: 16),
                      width: 36,
                      height: 4,
                      decoration: BoxDecoration(
                        color: EllaColors.bgTertiary,
                        borderRadius: BorderRadius.circular(2),
                      ),
                    ),
                    Text(
                      context.l10n.selectLanguage,
                      style: const TextStyle(
                        color: EllaColors.textPrimary,
                        fontSize: 17,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 16),
                    Expanded(
                      child: ListView.builder(
                        controller: scrollController,
                        itemCount: languages.length,
                        itemBuilder: (context, index) {
                          final entry = languages.entries.elementAt(index);
                          final isSelected = entry.value == currentLanguage;
                          return ListTile(
                            title: Text(
                              entry.key,
                              style: TextStyle(
                                color: isSelected ? EllaColors.primaryDark : EllaColors.textSecondary,
                                fontWeight: isSelected ? FontWeight.w500 : FontWeight.w400,
                              ),
                            ),
                            trailing: isSelected ? const Icon(Icons.check, color: EllaColors.primary, size: 20) : null,
                            onTap: _isUpdatingLanguage
                                ? null
                                : () async {
                                    setSheetState(() {
                                      currentLanguage = entry.value;
                                    });
                                    Navigator.pop(sheetContext);
                                    setState(() {
                                      _isUpdatingLanguage = true;
                                    });
                                    try {
                                      final success = await homeProvider.updateUserPrimaryLanguage(entry.value);
                                      if (success) {
                                        captureProvider.onRecordProfileSettingChanged();
                                        MixpanelManager().languageChanged(entry.value);
                                      }
                                    } finally {
                                      if (mounted) {
                                        setState(() {
                                          _isUpdatingLanguage = false;
                                        });
                                      }
                                    }
                                  },
                          );
                        },
                      ),
                    ),
                  ],
                );
              },
            );
          },
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    MixpanelManager().pageOpened('Language Settings');

    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: AppBar(
        backgroundColor: EllaColors.bgPrimary,
        foregroundColor: EllaColors.textPrimary,
        elevation: 0,
        leading: IconButton(
          icon: const FaIcon(FontAwesomeIcons.chevronLeft, color: EllaColors.textPrimary, size: 18),
          onPressed: () => Navigator.of(context).pop(),
        ),
        title: Text(
          context.l10n.languageTitle,
          style: const TextStyle(
            color: EllaColors.textPrimary,
            fontSize: 18,
            fontWeight: FontWeight.w600,
          ),
        ),
        centerTitle: true,
      ),
      body: Consumer4<HomeProvider, UserProvider, CaptureProvider, LocaleProvider>(
        builder: (context, homeProvider, userProvider, captureProvider, localeProvider, _) {
          return SingleChildScrollView(
            padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const SizedBox(height: 16),
                // App Interface Section
                _buildSectionHeader(context.l10n.appInterfaceSectionTitle),
                _buildAppInterfaceCard(localeProvider),
                const SizedBox(height: 24),
                // Speech & Transcription Section
                _buildSectionHeader(context.l10n.speechTranscriptionSectionTitle),
                _buildSpeechTranscriptionCard(homeProvider, userProvider, captureProvider),
                const SizedBox(height: 12),
                _buildHelperText(),
                const SizedBox(height: 32),
              ],
            ),
          );
        },
      ),
    );
  }
}
