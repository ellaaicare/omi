import 'dart:async';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:uuid/uuid.dart';

import 'package:omi/ella/ella_theme.dart';
import 'package:omi/ella/models/imessage_enrollment.dart';
import 'package:omi/ella/services/imessage_enrollment_api.dart';
import 'package:omi/ella/services/imessage_enrollment_attempt_store.dart';
import 'package:omi/ella/services/imessage_enrollment_controller.dart';
import 'package:omi/utils/l10n_extensions.dart';

bool isImessageEnrollmentSupportedPlatform({TargetPlatform? platform, bool? isWeb}) {
  return !(isWeb ?? kIsWeb) && (platform ?? defaultTargetPlatform) == TargetPlatform.iOS;
}

final _imessageEnrollmentSessionStore = ImessageEnrollmentSessionStore();
final _imessageEnrollmentAttemptStore = ImessageEnrollmentSecureAttemptStore();
ImessageEnrollmentController? _imessageEnrollmentController;

class ImessageEnrollmentPage extends StatefulWidget {
  const ImessageEnrollmentPage({
    super.key,
    this.controller,
    this.authorityChanges,
    this.platformSupported,
  });

  final ImessageEnrollmentController? controller;
  final Stream<String?>? authorityChanges;
  final bool? platformSupported;

  @override
  State<ImessageEnrollmentPage> createState() => _ImessageEnrollmentPageState();
}

class _ImessageEnrollmentPageState extends State<ImessageEnrollmentPage> {
  static final _e164 = RegExp(r'^\+[1-9][0-9]{7,14}$');

  late final ImessageEnrollmentController _controller;
  late final bool _usesDefaultController;
  late final bool _platformSupported;
  final _phoneController = TextEditingController();
  StreamSubscription<String?>? _authoritySubscription;
  bool _showConsent = false;
  bool _agreed = false;

  @override
  void initState() {
    super.initState();
    _platformSupported = widget.platformSupported ?? isImessageEnrollmentSupportedPlatform();
    if (!_platformSupported) {
      _usesDefaultController = false;
      return;
    }
    _usesDefaultController = widget.controller == null;
    _controller = widget.controller ?? _createController();
    _controller.addListener(_onControllerChanged);
    _controller.handleAuthorityChanged();
    final authorityChanges = widget.authorityChanges ??
        (_usesDefaultController ? FirebaseAuth.instance.authStateChanges().map((user) => user?.uid).distinct() : null);
    _authoritySubscription = authorityChanges?.listen(_onAuthorityChanged);
    unawaited(_controller.load());
  }

  ImessageEnrollmentController _createController() {
    final existing = _imessageEnrollmentController;
    if (existing != null) return existing;
    const api = ImessageEnrollmentApi();
    return _imessageEnrollmentController = ImessageEnrollmentController(
      gateway: api,
      consentGateway: api,
      authorityReader: () => FirebaseAuth.instance.currentUser?.uid,
      messagesLauncher: (uri) => launchUrl(uri, mode: LaunchMode.externalApplication),
      idGenerator: () => const Uuid().v4(),
      sessionStore: _imessageEnrollmentSessionStore,
      attemptStore: _imessageEnrollmentAttemptStore,
      appInfoReader: () async {
        final info = await PackageInfo.fromPlatform();
        return (version: info.version, buildNumber: info.buildNumber);
      },
    );
  }

  @override
  void dispose() {
    if (_platformSupported) {
      unawaited(_authoritySubscription?.cancel());
      _controller.removeListener(_onControllerChanged);
    }
    _phoneController.dispose();
    super.dispose();
  }

  void _onControllerChanged() {
    if (mounted) setState(() {});
  }

  void _onAuthorityChanged(String? _) {
    if (!_controller.handleAuthorityChanged()) return;
    if (mounted) {
      setState(() {
        _showConsent = false;
        _agreed = false;
        _phoneController.clear();
      });
    }
    unawaited(_controller.load());
  }

  @override
  Widget build(BuildContext context) {
    if (!_platformSupported) {
      return Scaffold(
        backgroundColor: EllaColors.bgPrimary,
        appBar: _buildAppBar(context),
        body: ListView(
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 40),
          children: [
            _EnrollmentCard(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _StatusHeading(
                    icon: Icons.phone_iphone_outlined,
                    title: context.l10n.ellaImessageUnavailableTitle,
                    color: EllaColors.textTertiary,
                  ),
                  const SizedBox(height: 10),
                  Text(
                    context.l10n.ellaImessageUnavailableBody,
                    style: const TextStyle(fontSize: 15, height: 1.35, color: EllaColors.textSecondary),
                  ),
                ],
              ),
            ),
          ],
        ),
      );
    }
    return Scaffold(
      backgroundColor: EllaColors.bgPrimary,
      appBar: _buildAppBar(context),
      body: RefreshIndicator(
        color: EllaColors.primary,
        onRefresh: _controller.load,
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 40),
          children: [
            Text(
              context.l10n.ellaImessageIntro,
              style: const TextStyle(fontSize: 16, height: 1.4, color: EllaColors.textSecondary),
            ),
            const SizedBox(height: 20),
            _buildStatusCard(),
            if (_controller.failure != null && !_controller.consentRevocationPending) ...[
              const SizedBox(height: 12),
              _buildFailureCard(_controller.failure!),
            ],
            if (_showConsent) ...[const SizedBox(height: 16), _buildConsentCard()],
          ],
        ),
      ),
    );
  }

  AppBar _buildAppBar(BuildContext context) {
    return AppBar(
      backgroundColor: EllaColors.bgPrimary,
      elevation: 0,
      leading: IconButton(
        tooltip: MaterialLocalizations.of(context).backButtonTooltip,
        onPressed: () => Navigator.of(context).pop(),
        icon: const Icon(Icons.arrow_back, color: EllaColors.textPrimary),
      ),
      title: Text(
        context.l10n.ellaImessageTitle,
        style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
      ),
    );
  }

  Widget _buildStatusCard() {
    if (_controller.status == null && _controller.loading) {
      return const _EnrollmentCard(
        child: Center(
          child: Padding(
            padding: EdgeInsets.symmetric(vertical: 28),
            child: CircularProgressIndicator(color: EllaColors.primary),
          ),
        ),
      );
    }

    final status = _controller.status;
    if (status == null) {
      return _EnrollmentCard(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _StatusHeading(
              icon: Icons.cloud_off_outlined,
              title: context.l10n.ellaImessageUnavailableTitle,
              color: EllaColors.textTertiary,
            ),
            const SizedBox(height: 10),
            Text(
              context.l10n.ellaImessageUnavailableBody,
              style: const TextStyle(fontSize: 15, height: 1.35, color: EllaColors.textSecondary),
            ),
            const SizedBox(height: 16),
            _PrimaryButton(
              label: context.l10n.ellaImessageRetry,
              icon: Icons.refresh,
              onPressed: _controller.loading ? null : _controller.load,
            ),
          ],
        ),
      );
    }

    return switch (status.state) {
      ImessageEnrollmentState.notConnected => _buildNotConnected(status),
      ImessageEnrollmentState.verificationPending => _buildPending(status),
      ImessageEnrollmentState.ready => status.isReady ? _buildReady(status) : _buildFeatureUnavailable(status),
      ImessageEnrollmentState.temporarilyUnavailable => _buildTemporarilyUnavailable(status),
      ImessageEnrollmentState.revoked => _buildRevoked(status),
    };
  }

  Widget _buildNotConnected(ImessageEnrollmentStatus status) {
    return _EnrollmentCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _StatusHeading(
            icon: Icons.chat_bubble_outline,
            title: context.l10n.ellaImessageNotConnectedTitle,
            color: EllaColors.textTertiary,
          ),
          const SizedBox(height: 10),
          Text(
            context.l10n.ellaImessageNotConnectedBody,
            style: const TextStyle(fontSize: 15, height: 1.35, color: EllaColors.textSecondary),
          ),
          const SizedBox(height: 16),
          _PrimaryButton(
            label: context.l10n.ellaImessageSetUp,
            icon: Icons.link,
            onPressed: _controller.loading ? null : _beginConsent,
          ),
        ],
      ),
    );
  }

  Widget _buildPending(ImessageEnrollmentStatus status) {
    return _EnrollmentCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _StatusHeading(
            icon: Icons.mark_chat_unread_outlined,
            title: context.l10n.ellaImessagePendingTitle,
            color: EllaColors.warning,
          ),
          const SizedBox(height: 10),
          Text(
            context.l10n.ellaImessagePendingBody,
            style: const TextStyle(fontSize: 15, height: 1.35, color: EllaColors.textSecondary),
          ),
          if (status.assignedDestination != null) ...[
            const SizedBox(height: 12),
            Text(
              context.l10n.ellaImessageAssignedDestination(status.assignedDestination!),
              style: const TextStyle(fontSize: 14, color: EllaColors.textTertiary),
            ),
          ],
          const SizedBox(height: 16),
          if (_controller.canOpenMessages)
            _PrimaryButton(
              label: context.l10n.ellaImessageOpenMessages,
              icon: Icons.open_in_new,
              onPressed: _controller.loading ? null : _controller.openMessages,
            )
          else if (_controller.canRetryPendingStart)
            _PrimaryButton(
              label: context.l10n.ellaImessageRetry,
              icon: Icons.replay,
              onPressed: _controller.loading ? null : _controller.retryPendingStart,
            ),
          const SizedBox(height: 8),
          _SecondaryButton(
            label: context.l10n.ellaImessageCheckVerification,
            onPressed: _controller.loading ? null : _controller.load,
          ),
          if (_controller.canDisconnect) ...[
            const SizedBox(height: 8),
            _buildDisconnectButton(),
          ],
        ],
      ),
    );
  }

  Widget _buildReady(ImessageEnrollmentStatus status) {
    return _EnrollmentCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _StatusHeading(icon: Icons.check_circle_outline, title: context.l10n.ellaImessageReadyTitle),
          const SizedBox(height: 10),
          Text(
            context.l10n.ellaImessageReadyBody,
            style: const TextStyle(fontSize: 15, height: 1.35, color: EllaColors.textSecondary),
          ),
          if (status.lastVerifiedAt != null) ...[
            const SizedBox(height: 10),
            Text(
              context.l10n.ellaImessageLastVerified(_formatDateTime(status.lastVerifiedAt!)),
              style: const TextStyle(fontSize: 13, color: EllaColors.textTertiary),
            ),
          ],
          const SizedBox(height: 14),
          Text(
            context.l10n.ellaImessageTextOnly,
            style: const TextStyle(fontSize: 13, height: 1.35, color: EllaColors.textTertiary),
          ),
          const SizedBox(height: 16),
          _buildDisconnectButton(),
        ],
      ),
    );
  }

  Widget _buildFeatureUnavailable(ImessageEnrollmentStatus status) {
    return _EnrollmentCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _StatusHeading(
            icon: Icons.info_outline,
            title: context.l10n.ellaImessageUnavailableTitle,
            color: EllaColors.textTertiary,
          ),
          const SizedBox(height: 10),
          Text(
            context.l10n.ellaImessageFeatureUnavailableBody,
            style: const TextStyle(fontSize: 15, height: 1.35, color: EllaColors.textSecondary),
          ),
          const SizedBox(height: 16),
          _PrimaryButton(
            label: context.l10n.ellaImessageRetry,
            icon: Icons.refresh,
            onPressed: _controller.loading ? null : _controller.load,
          ),
          if (_controller.canDisconnect) ...[
            const SizedBox(height: 8),
            _buildDisconnectButton(),
          ],
        ],
      ),
    );
  }

  Widget _buildTemporarilyUnavailable(ImessageEnrollmentStatus status) {
    return _EnrollmentCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _StatusHeading(
            icon: Icons.cloud_off_outlined,
            title: context.l10n.ellaImessageUnavailableTitle,
            color: EllaColors.warning,
          ),
          const SizedBox(height: 10),
          Text(
            context.l10n.ellaImessageUnavailableBody,
            style: const TextStyle(fontSize: 15, height: 1.35, color: EllaColors.textSecondary),
          ),
          if (status.supportCode != null) ...[
            const SizedBox(height: 10),
            SelectableText(
              context.l10n.ellaImessageSupportCode(status.supportCode!),
              style: const TextStyle(fontSize: 13, color: EllaColors.textTertiary),
            ),
          ],
          const SizedBox(height: 16),
          _PrimaryButton(
            label: context.l10n.ellaImessageRetry,
            icon: Icons.refresh,
            onPressed: _controller.loading ? null : _controller.load,
          ),
          if (_controller.canDisconnect) ...[
            const SizedBox(height: 8),
            _buildDisconnectButton(),
          ],
        ],
      ),
    );
  }

  Widget _buildRevoked(ImessageEnrollmentStatus status) {
    return _EnrollmentCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _StatusHeading(
            icon: Icons.link_off,
            title: context.l10n.ellaImessageRevokedTitle,
            color: EllaColors.textTertiary,
          ),
          const SizedBox(height: 10),
          Text(
            _controller.consentRevocationPending
                ? context.l10n.ellaImessageConsentRevocationPendingBody
                : context.l10n.ellaImessageRevokedBody,
            style: const TextStyle(fontSize: 15, height: 1.35, color: EllaColors.textSecondary),
          ),
          const SizedBox(height: 16),
          _PrimaryButton(
            label: _controller.consentRevocationPending
                ? context.l10n.ellaImessageFinishDisconnect
                : context.l10n.ellaImessageReconnect,
            icon: _controller.consentRevocationPending ? Icons.sync : Icons.link,
            onPressed: _controller.loading
                ? null
                : _controller.consentRevocationPending
                    ? _controller.revoke
                    : _beginConsent,
          ),
        ],
      ),
    );
  }

  Widget _buildDisconnectButton() {
    return _SecondaryButton(
      label: context.l10n.ellaImessageDisconnect,
      destructive: true,
      onPressed: _controller.loading ? null : _controller.revoke,
    );
  }

  Widget _buildFailureCard(ImessageEnrollmentFailure failure) {
    final message = switch (failure.kind) {
      ImessageEnrollmentFailureKind.unauthenticated => context.l10n.ellaImessageErrorSignIn,
      ImessageEnrollmentFailureKind.rateLimited => context.l10n.ellaImessageErrorRateLimited,
      ImessageEnrollmentFailureKind.consentUnavailable => context.l10n.ellaImessageErrorConsentUnavailable,
      ImessageEnrollmentFailureKind.proofExpired => context.l10n.ellaImessageErrorProofExpired,
      ImessageEnrollmentFailureKind.authorityChanged => context.l10n.ellaImessageErrorAccountChanged,
      ImessageEnrollmentFailureKind.messagesUnavailable => context.l10n.ellaImessageErrorMessages,
      _ => context.l10n.ellaImessageErrorGeneric,
    };

    return Semantics(
      liveRegion: true,
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: EllaColors.error.withValues(alpha: 0.08),
          borderRadius: BorderRadius.circular(EllaSizes.radiusMedium),
          border: Border.all(color: EllaColors.error.withValues(alpha: 0.35)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(message, style: const TextStyle(fontSize: 14, height: 1.35, color: EllaColors.error)),
            if (failure.supportCode != null) ...[
              const SizedBox(height: 6),
              SelectableText(
                context.l10n.ellaImessageSupportCode(failure.supportCode!),
                style: const TextStyle(fontSize: 13, color: EllaColors.textSecondary),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildConsentCard() {
    final policy = _controller.consentPolicy;
    if (policy == null) {
      return _EnrollmentCard(
        child: _controller.loading
            ? const Center(
                child: Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: CircularProgressIndicator(color: EllaColors.primary),
                ),
              )
            : _PrimaryButton(
                label: context.l10n.ellaImessageRetry,
                icon: Icons.refresh,
                onPressed: _controller.loadConsentPolicy,
              ),
      );
    }
    final validPhone = _e164.hasMatch(_phoneController.text.trim());
    final recipients = policy.recipients.join(', ');
    final dataClasses = policy.dataClasses.join(', ');
    return _EnrollmentCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            context.l10n.ellaImessageConsentTitle,
            style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
          ),
          const SizedBox(height: 10),
          Text(
            context.l10n.ellaImessageConsentFlow(recipients),
            style: const TextStyle(fontSize: 15, height: 1.4, color: EllaColors.textSecondary),
          ),
          const SizedBox(height: 10),
          Text(
            context.l10n.ellaImessageConsentData(dataClasses),
            style: const TextStyle(fontSize: 14, height: 1.4, color: EllaColors.textSecondary),
          ),
          const SizedBox(height: 10),
          Text(
            context.l10n.ellaImessageTextOnly,
            style: const TextStyle(fontSize: 13, height: 1.35, color: EllaColors.textTertiary),
          ),
          const SizedBox(height: 18),
          TextField(
            controller: _phoneController,
            keyboardType: TextInputType.phone,
            autofillHints: const [AutofillHints.telephoneNumber],
            onChanged: (_) => setState(() {}),
            decoration: InputDecoration(
              labelText: context.l10n.ellaImessagePhoneLabel,
              hintText: context.l10n.ellaImessagePhoneHint,
              helperText: context.l10n.ellaImessagePhoneHelp,
              errorText: _phoneController.text.isNotEmpty && !validPhone ? context.l10n.ellaImessagePhoneInvalid : null,
              border: OutlineInputBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusMedium)),
            ),
          ),
          const SizedBox(height: 10),
          CheckboxListTile(
            contentPadding: EdgeInsets.zero,
            controlAffinity: ListTileControlAffinity.leading,
            activeColor: EllaColors.primary,
            value: _agreed,
            onChanged: _controller.loading ? null : (value) => setState(() => _agreed = value ?? false),
            title: Text(
              context.l10n.ellaImessageConsentAgreement(recipients),
              style: const TextStyle(fontSize: 14, height: 1.35, color: EllaColors.textPrimary),
            ),
          ),
          const SizedBox(height: 8),
          _PrimaryButton(
            label: context.l10n.ellaImessageAgreeContinue,
            icon: Icons.lock_outline,
            onPressed: !_agreed || !validPhone || _controller.loading ? null : _startEnrollment,
          ),
          const SizedBox(height: 8),
          _SecondaryButton(label: context.l10n.ellaImessageNotNow, onPressed: _cancelConsent),
        ],
      ),
    );
  }

  Future<void> _beginConsent() async {
    setState(() {
      _showConsent = true;
      _agreed = false;
      _phoneController.clear();
    });
    await _controller.loadConsentPolicy();
  }

  Future<void> _startEnrollment() async {
    await _controller.start(_phoneController.text.trim());
    if (!mounted) return;
    if (_controller.status?.state == ImessageEnrollmentState.verificationPending) {
      setState(() {
        _showConsent = false;
        _agreed = false;
        _phoneController.clear();
      });
    }
  }

  Future<void> _cancelConsent() async {
    await _controller.declineConsent();
    if (!mounted || _controller.failure != null) return;
    setState(() {
      _showConsent = false;
      _agreed = false;
      _phoneController.clear();
    });
  }

  String _formatDateTime(DateTime value) {
    final local = value.toLocal();
    final localizations = MaterialLocalizations.of(context);
    final date = localizations.formatMediumDate(local);
    final time = localizations.formatTimeOfDay(TimeOfDay.fromDateTime(local));
    return '$date, $time';
  }
}

class _EnrollmentCard extends StatelessWidget {
  const _EnrollmentCard({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: EllaColors.bgSecondary,
        borderRadius: BorderRadius.circular(EllaSizes.radiusLarge),
        border: Border.all(color: EllaColors.textDisabled.withValues(alpha: 0.35)),
      ),
      child: child,
    );
  }
}

class _StatusHeading extends StatelessWidget {
  const _StatusHeading({required this.icon, required this.title, this.color = EllaColors.primary});

  final IconData icon;
  final String title;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      header: true,
      child: Row(
        children: [
          Icon(icon, color: color, size: 24),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              title,
              style: const TextStyle(fontSize: 19, fontWeight: FontWeight.w700, color: EllaColors.textPrimary),
            ),
          ),
        ],
      ),
    );
  }
}

class _PrimaryButton extends StatelessWidget {
  const _PrimaryButton({required this.label, required this.icon, required this.onPressed});

  final String label;
  final IconData icon;
  final FutureOr<void> Function()? onPressed;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      child: ElevatedButton.icon(
        onPressed: onPressed == null ? null : () => onPressed!(),
        icon: Icon(icon),
        label: Text(label),
        style: ElevatedButton.styleFrom(
          backgroundColor: EllaColors.primary,
          foregroundColor: Colors.white,
          disabledBackgroundColor: EllaColors.textDisabled,
          minimumSize: const Size.fromHeight(50),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(EllaSizes.radiusMedium)),
        ),
      ),
    );
  }
}

class _SecondaryButton extends StatelessWidget {
  const _SecondaryButton({required this.label, required this.onPressed, this.destructive = false});

  final String label;
  final FutureOr<void> Function()? onPressed;
  final bool destructive;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      child: TextButton(
        onPressed: onPressed == null ? null : () => onPressed!(),
        style: TextButton.styleFrom(
          foregroundColor: destructive ? EllaColors.error : EllaColors.primary,
          minimumSize: const Size.fromHeight(48),
        ),
        child: Text(label),
      ),
    );
  }
}
