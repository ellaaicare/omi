import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'package:provider/provider.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/services/ella_entitlement_service.dart';
import 'package:omi/ella/services/ella_invite_link_controller.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';
import 'package:omi/utils/auth_utils.dart';
import 'package:omi/utils/l10n_extensions.dart';

class EllaEntitlementGatePage extends StatefulWidget {
  const EllaEntitlementGatePage({
    super.key,
    required this.readyChild,
    this.startOnMount = true,
    this.onSignOutOverride,
  });

  final Widget readyChild;
  final bool startOnMount;
  final VoidCallback? onSignOutOverride;

  @override
  State<EllaEntitlementGatePage> createState() => _EllaEntitlementGatePageState();
}

class _EllaEntitlementGatePageState extends State<EllaEntitlementGatePage> {
  final TextEditingController _codeController = TextEditingController();
  bool _showCodeEntry = false;

  @override
  void initState() {
    super.initState();
    EllaInviteLinkController.instance.addListener(_acceptPendingLink);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final pendingCode = EllaInviteLinkController.instance.consume();
      if (pendingCode.isNotEmpty) {
        _setCode(pendingCode);
      } else {
        final existingCode = context.read<EllaEntitlementProvider>().inviteCode;
        if (existingCode.isNotEmpty) _setCode(existingCode);
      }
      if (widget.startOnMount) {
        context.read<EllaEntitlementProvider>().load(prefilledCode: pendingCode);
      }
    });
  }

  @override
  void dispose() {
    EllaInviteLinkController.instance.removeListener(_acceptPendingLink);
    _codeController.dispose();
    super.dispose();
  }

  void _acceptPendingLink() {
    if (!mounted) return;
    final code = EllaInviteLinkController.instance.consume();
    if (code.isEmpty) return;
    _setCode(code);
    context.read<EllaEntitlementProvider>().acceptInviteLink(code);
  }

  void _setCode(String code) {
    _codeController.value = TextEditingValue(
      text: code,
      selection: TextSelection.collapsed(offset: code.length),
    );
    setState(() => _showCodeEntry = true);
  }

  Future<void> _pasteCode() async {
    final data = await Clipboard.getData(Clipboard.kTextPlain);
    final code = normalizeEllaInviteCode(data?.text ?? '');
    if (code.isEmpty || !mounted) return;
    _setCode(code);
    context.read<EllaEntitlementProvider>().acceptInviteLink(code);
  }

  Future<void> _redeem() async {
    FocusScope.of(context).unfocus();
    await context.read<EllaEntitlementProvider>().redeem(_codeController.text);
  }

  Future<void> _signOut() async {
    if (widget.onSignOutOverride != null) {
      widget.onSignOutOverride!();
      return;
    }
    context.read<EllaEntitlementProvider>().reset();
    await signOutAndClearUserData(context);
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<EllaEntitlementProvider>(
      builder: (context, provider, _) {
        if (provider.isActive) return widget.readyChild;

        if (provider.state == EllaEntitlementLoadState.loading || provider.state == EllaEntitlementLoadState.idle) {
          return _AccessShell(
            icon: const Padding(
              padding: EdgeInsets.all(21),
              child: CircularProgressIndicator(color: EllaColors.tealDeep, strokeWidth: 3),
            ),
            title: context.l10n.ellaAccessCheckingTitle,
            body: context.l10n.ellaAccessCheckingBody,
            onSignOut: _signOut,
          );
        }

        if (provider.state == EllaEntitlementLoadState.unavailable) {
          return _AccessShell(
            icon: const Icon(Icons.cloud_outlined, color: EllaColors.tealDeep, size: 34),
            title: context.l10n.ellaAccessUnavailableTitle,
            body: context.l10n.ellaAccessUnavailableBody,
            primaryLabel: context.l10n.retry,
            onPrimary: provider.retry,
            onSignOut: _signOut,
          );
        }

        final entitlement = provider.entitlement;
        if (entitlement?.status == EllaEntitlementStatus.suspended) {
          return _AccessShell(
            icon: const Icon(Icons.pause_circle_outline_rounded, color: EllaColors.tealDeep, size: 36),
            title: context.l10n.ellaAccessPausedTitle,
            body: context.l10n.ellaAccessPausedBody,
            primaryLabel: context.l10n.retry,
            onPrimary: provider.retry,
            onSignOut: _signOut,
          );
        }

        final showWaitlist = entitlement?.status == EllaEntitlementStatus.none &&
            provider.inviteError != EllaInviteRedemptionError.invalid &&
            provider.inviteError != EllaInviteRedemptionError.expired &&
            provider.inviteError != EllaInviteRedemptionError.rateLimited &&
            !_showCodeEntry &&
            provider.inviteCode.isEmpty;
        if ((showWaitlist || provider.inviteError == EllaInviteRedemptionError.capacity) && !_showCodeEntry) {
          return _AccessShell(
            icon: const Icon(Icons.favorite_outline_rounded, color: EllaColors.tealDeep, size: 34),
            title: context.l10n.ellaWaitlistTitle,
            body: provider.inviteError == EllaInviteRedemptionError.capacity
                ? context.l10n.ellaInviteCapacityBody
                : context.l10n.ellaWaitlistBody,
            primaryLabel: context.l10n.ellaEnterInviteCode,
            onPrimary: () => setState(() => _showCodeEntry = true),
            onSignOut: _signOut,
          );
        }

        return _AccessShell(
          icon: const Icon(Icons.mail_outline_rounded, color: EllaColors.tealDeep, size: 34),
          title: provider.inviteCode.isNotEmpty ? context.l10n.ellaInviteReadyTitle : context.l10n.ellaInviteEntryTitle,
          body: provider.inviteCode.isNotEmpty ? context.l10n.ellaInviteReadyBody : context.l10n.ellaInviteEntryBody,
          onSignOut: _signOut,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              if (provider.inviteError != null) ...[
                _InviteMessage(reason: provider.inviteError!),
                const SizedBox(height: 16),
              ],
              Semantics(
                textField: true,
                label: context.l10n.ellaInviteCodeFieldLabel,
                child: TextField(
                  controller: _codeController,
                  autocorrect: false,
                  enableSuggestions: false,
                  textCapitalization: TextCapitalization.characters,
                  keyboardType: TextInputType.visiblePassword,
                  style: EllaTextStyles.body.copyWith(
                    fontSize: 22,
                    fontWeight: FontWeight.w700,
                    fontFamily: 'monospace',
                    letterSpacing: 4,
                    color: EllaColors.ink,
                  ),
                  inputFormatters: [
                    FilteringTextInputFormatter.allow(RegExp('[A-Za-z0-9 -]')),
                    TextInputFormatter.withFunction((oldValue, newValue) {
                      final normalized = normalizeEllaInviteCode(newValue.text);
                      return TextEditingValue(
                        text: normalized,
                        selection: TextSelection.collapsed(offset: normalized.length),
                      );
                    }),
                  ],
                  decoration: InputDecoration(
                    labelText: context.l10n.ellaInviteCodeFieldLabel,
                    hintText: context.l10n.ellaInviteCodeHint,
                    hintStyle: EllaTextStyles.body.copyWith(color: EllaColors.inkSoft, letterSpacing: 1),
                    filled: true,
                    fillColor: EllaColors.paper,
                    contentPadding: const EdgeInsets.symmetric(horizontal: 18, vertical: 20),
                    enabledBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                      borderSide: const BorderSide(color: EllaColors.cardEdge),
                    ),
                    focusedBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
                      borderSide: const BorderSide(color: EllaColors.tealDeep, width: 2),
                    ),
                  ),
                  onChanged: provider.acceptInviteLink,
                  onSubmitted: (_) => _redeem(),
                ),
              ),
              const SizedBox(height: 10),
              Text(context.l10n.ellaInviteCodeHelp, style: EllaTextStyles.caption, textAlign: TextAlign.center),
              const SizedBox(height: 12),
              OutlinedButton.icon(
                onPressed: _pasteCode,
                icon: const Icon(Icons.content_paste_rounded),
                label: Text(context.l10n.ellaPasteInviteCode),
                style: OutlinedButton.styleFrom(
                  foregroundColor: EllaColors.tealDeep,
                  minimumSize: const Size.fromHeight(EllaSizes.minTouchTarget),
                  side: const BorderSide(color: EllaColors.cardEdge),
                ),
              ),
              const SizedBox(height: 12),
              FilledButton(
                onPressed: provider.state == EllaEntitlementLoadState.redeeming ? null : _redeem,
                style: FilledButton.styleFrom(
                  backgroundColor: EllaColors.tealDeep,
                  foregroundColor: EllaColors.paper,
                  minimumSize: const Size.fromHeight(EllaSizes.minTouchTarget),
                ),
                child: provider.state == EllaEntitlementLoadState.redeeming
                    ? const SizedBox(
                        width: 22,
                        height: 22,
                        child: CircularProgressIndicator(strokeWidth: 2, color: EllaColors.paper),
                      )
                    : Text(context.l10n.ellaConfirmInviteCode),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _InviteMessage extends StatelessWidget {
  const _InviteMessage({required this.reason});

  final EllaInviteRedemptionError reason;

  @override
  Widget build(BuildContext context) {
    final message = switch (reason) {
      EllaInviteRedemptionError.invalid => context.l10n.ellaInviteInvalidBody,
      EllaInviteRedemptionError.expired => context.l10n.ellaInviteExpiredBody,
      EllaInviteRedemptionError.capacity => context.l10n.ellaInviteCapacityBody,
      EllaInviteRedemptionError.rateLimited => context.l10n.ellaInviteRateLimitedBody,
    };
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: EllaColors.cardDeep,
        borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
        border: Border.all(color: EllaColors.cardEdge),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.info_outline_rounded, color: EllaColors.tealDeep),
          const SizedBox(width: 10),
          Expanded(
            child: Text(message, style: EllaTextStyles.caption.copyWith(color: EllaColors.ink)),
          ),
        ],
      ),
    );
  }
}

class _AccessShell extends StatelessWidget {
  const _AccessShell({
    required this.icon,
    required this.title,
    required this.body,
    required this.onSignOut,
    this.primaryLabel,
    this.onPrimary,
    this.child,
  });

  final Widget icon;
  final String title;
  final String body;
  final String? primaryLabel;
  final VoidCallback? onPrimary;
  final VoidCallback onSignOut;
  final Widget? child;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: EllaColors.paper,
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(EllaSizes.screenPadding),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 480),
              child: EllaCardSurface(
                child: Padding(
                  padding: const EdgeInsets.all(28),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Container(
                        width: 76,
                        height: 76,
                        decoration: const BoxDecoration(color: EllaColors.cardDeep, shape: BoxShape.circle),
                        child: Center(child: icon),
                      ),
                      const SizedBox(height: 24),
                      Text(title, style: EllaTextStyles.display, textAlign: TextAlign.center),
                      const SizedBox(height: 12),
                      Text(body, style: EllaTextStyles.secondary, textAlign: TextAlign.center),
                      if (child != null) ...[const SizedBox(height: 24), child!],
                      if (primaryLabel != null && onPrimary != null) ...[
                        const SizedBox(height: 24),
                        SizedBox(
                          width: double.infinity,
                          child: FilledButton(
                            onPressed: onPrimary,
                            style: FilledButton.styleFrom(
                              backgroundColor: EllaColors.tealDeep,
                              foregroundColor: EllaColors.paper,
                              minimumSize: const Size.fromHeight(EllaSizes.minTouchTarget),
                            ),
                            child: Text(primaryLabel!),
                          ),
                        ),
                      ],
                      const SizedBox(height: 12),
                      TextButton(
                        onPressed: onSignOut,
                        child: Text(
                          context.l10n.ellaSettingsSignOut,
                          style: const TextStyle(color: EllaColors.inkSoft),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
