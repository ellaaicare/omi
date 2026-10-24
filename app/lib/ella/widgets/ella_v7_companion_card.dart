import 'package:flutter/material.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/widgets/ella_v7_companion_preferences.dart';
import 'package:omi/ella/widgets/ella_v7_surfaces.dart';
import 'package:omi/utils/l10n_extensions.dart';

Future<void> showEllaV7CompanionEditor(BuildContext context) {
  final preferences = SharedPreferencesUtil();
  final uid = preferences.uid;
  final generation = preferences.terminalAccountConsentAuthorityGeneration;
  return showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: Colors.transparent,
      showDragHandle: false,
      builder: (_) => _CompanionEditor(uid: uid, generation: generation));
}

class EllaV7CompanionCard extends StatelessWidget {
  const EllaV7CompanionCard({super.key});
  @override
  Widget build(BuildContext context) => ListenableBuilder(
      listenable: EllaV7CompanionPreferences.shared,
      builder: (context, _) {
        final name = EllaV7CompanionPreferences.shared.name;
        return Semantics(
            container: true,
            button: true,
            enabled: true,
            excludeSemantics: true,
            onTap: () => showEllaV7CompanionEditor(context),
            label:
                '${name.isEmpty ? context.l10n.ellaV7CompanionDefaultName : name}. ${context.l10n.ellaV7CompanionCustomize}',
            child: EllaV7Glass(
                child: Material(
                    color: Colors.transparent,
                    child: InkWell(
                        key: const Key('v7-companion-customize'),
                        onTap: () => showEllaV7CompanionEditor(context),
                        child: Padding(
                            padding: const EdgeInsets.all(20),
                            child: Row(children: [
                              const EllaV7Lume(size: 86),
                              const SizedBox(width: 16),
                              Expanded(
                                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                                Text(name.isEmpty ? context.l10n.ellaV7CompanionDefaultName : name,
                                    style: EllaTypography.of(context).noteBody),
                                Text(context.l10n.ellaV7CompanionCustomize, style: EllaTypography.of(context).body),
                              ])),
                              const Icon(Icons.chevron_right_rounded),
                            ]))))));
      });
}

class _CompanionEditor extends StatefulWidget {
  const _CompanionEditor({required this.uid, required this.generation});
  final String uid;
  final int generation;
  @override
  State<_CompanionEditor> createState() => _CompanionEditorState();
}

class _CompanionEditorState extends State<_CompanionEditor> {
  bool get _current =>
      SharedPreferencesUtil().uid == widget.uid &&
      SharedPreferencesUtil().terminalAccountConsentAuthorityGeneration == widget.generation;
  @override
  void initState() {
    super.initState();
    SharedPreferencesUtil.aiConsentAuthorityChanges.addListener(_authorityChanged);
    if (_current) {
      _shape = EllaV7CompanionPreferences.shared.styleIndex;
      _color = EllaV7CompanionPreferences.shared.colorIndex;
    }
  }

  void _authorityChanged() {
    if (mounted) setState(() {});
  }

  TextEditingController? _nameController;
  TextEditingController get _name => _nameController!;
  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final stored = _current ? EllaV7CompanionPreferences.shared.name : '';
    _nameController ??= TextEditingController(text: stored.isEmpty ? context.l10n.ellaV7CompanionDefaultName : stored);
  }

  int _shape = 0;
  int _color = 0;
  bool _saving = false, _failed = false;
  @override
  void dispose() {
    SharedPreferencesUtil.aiConsentAuthorityChanges.removeListener(_authorityChanged);
    _nameController?.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (!_current) return;
    setState(() => _saving = true);
    final saved = await EllaV7CompanionPreferences.shared
        .save(uid: widget.uid, name: _name.text, colorIndex: _color, styleIndex: _shape);
    if (!mounted || !_current) return;
    if (saved) {
      Navigator.of(context).pop();
    } else {
      setState(() {
        _saving = false;
        _failed = true;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final palette = EllaPalette.of(context);
    if (!_current) {
      return EllaV7Glass(
          child: SafeArea(
              child: Column(mainAxisSize: MainAxisSize.min, children: [
        TextButton(
            style: TextButton.styleFrom(foregroundColor: palette.ink, minimumSize: const Size(48, 48)),
            onPressed: () => Navigator.of(context).pop(),
            child: Text(context.l10n.close)),
        FilledButton(
            key: const Key('v7-companion-save'),
            style: FilledButton.styleFrom(
                disabledForegroundColor: palette.ink,
                disabledBackgroundColor: palette.card,
                minimumSize: const Size(48, 56)),
            onPressed: null,
            child: Text(context.l10n.save)),
      ])));
    }
    return EllaV7Glass(
        radius: 38,
        child: SafeArea(
            top: false,
            child: SingleChildScrollView(
                padding: EdgeInsets.fromLTRB(24, 16, 24, 24 + MediaQuery.viewInsetsOf(context).bottom),
                child:
                    Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
                  Row(children: [
                    Expanded(
                        child: Text(context.l10n.ellaCompanionEditorTitle,
                            style: TextStyle(
                                fontFamily: EllaTextStyles.uiFont,
                                fontSize: 22,
                                fontWeight: FontWeight.w600,
                                color: palette.ink))),
                    IconButton(
                        tooltip: context.l10n.close,
                        onPressed: () => Navigator.of(context).pop(),
                        icon: const Icon(Icons.close)),
                  ]),
                  Center(child: EllaV7Lume(colorIndex: _color, styleIndex: _shape)),
                  TextField(
                      key: const Key('v7-companion-name'),
                      controller: _name,
                      maxLength: 40,
                      enabled: !_saving,
                      style: TextStyle(color: palette.ink, fontSize: 17),
                      cursorColor: palette.ink,
                      decoration: InputDecoration(
                          labelText: context.l10n.nameLabel,
                          hintText: context.l10n.ellaV7CompanionDefaultName,
                          labelStyle: TextStyle(color: palette.inkSoft, fontSize: 17),
                          hintStyle: TextStyle(color: palette.inkSoft, fontSize: 17),
                          counterStyle: TextStyle(color: palette.inkSoft, fontSize: 16),
                          filled: true,
                          fillColor: palette.card,
                          enabledBorder: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(20), borderSide: BorderSide(color: palette.cardEdge)),
                          focusedBorder: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(20), borderSide: BorderSide(color: palette.ink)))),
                  Text(context.l10n.ellaV7CompanionShape, style: EllaTypography.of(context).body),
                  Wrap(spacing: 8, runSpacing: 8, children: [
                    for (var i = 0; i < 3; i++)
                      ChoiceChip(
                          key: Key('v7-companion-shape-$i'),
                          selected: i == _shape,
                          showCheckmark: false,
                          backgroundColor: palette.card,
                          selectedColor: palette.ink,
                          side: BorderSide(color: palette.cardEdge),
                          materialTapTargetSize: MaterialTapTargetSize.padded,
                          avatar: EllaV7Lume(size: 30, colorIndex: _color, styleIndex: i, animate: false),
                          label: Text(
                              switch (i) {
                                1 => context.l10n.ellaV7CompanionStyleLeaf,
                                2 => context.l10n.ellaV7CompanionStyleStar,
                                _ => context.l10n.ellaV7CompanionStyleLume,
                              },
                              style: TextStyle(fontSize: 16, color: i == _shape ? palette.paper : palette.ink)),
                          onSelected: _saving ? null : (_) => setState(() => _shape = i)),
                  ]),
                  const SizedBox(height: 12),
                  Text(context.l10n.color, style: EllaTypography.of(context).body),
                  Wrap(spacing: 8, runSpacing: 8, children: [
                    for (var i = 0; i < 3; i++)
                      ChoiceChip(
                          key: Key('v7-companion-color-$i'),
                          selected: i == _color,
                          materialTapTargetSize: MaterialTapTargetSize.padded,
                          showCheckmark: false,
                          backgroundColor: EllaPalette.of(context).card,
                          selectedColor: EllaPalette.of(context).ink,
                          side: BorderSide(color: EllaPalette.of(context).cardEdge),
                          avatar: EllaV7Lume(size: 30, colorIndex: i, styleIndex: _shape, animate: false),
                          label: Text(
                              switch (i) {
                                1 => context.l10n.ellaV7CompanionColorWarm,
                                2 => context.l10n.ellaV7CompanionColorCool,
                                _ => context.l10n.ellaV7CompanionColorOriginal,
                              },
                              style: TextStyle(
                                  fontSize: 16,
                                  color: i == _color ? EllaPalette.of(context).paper : EllaPalette.of(context).ink)),
                          onSelected: _saving ? null : (_) => setState(() => _color = i)),
                  ]),
                  if (_failed) Text(context.l10n.ellaV7CompanionSaveFailed, style: EllaTypography.of(context).body),
                  const SizedBox(height: 20),
                  FilledButton(
                      key: const Key('v7-companion-save'),
                      style: FilledButton.styleFrom(
                          minimumSize: const Size(44, 56),
                          backgroundColor: palette.ink,
                          foregroundColor: palette.paper),
                      onPressed: _saving ? null : _save,
                      child: Text(context.l10n.save, style: const TextStyle(fontSize: 16))),
                ]))));
  }
}
