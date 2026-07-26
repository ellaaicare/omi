import 'package:omi/ella/services/ella_entitlement_service.dart';

class EllaAccessDemoTransport implements EllaEntitlementTransport {
  EllaAccessDemoTransport({required this.fetchResult, this.redeemResult, this.redeemError});

  final EllaEntitlement fetchResult;
  final EllaEntitlement? redeemResult;
  final EllaInviteRedemptionError? redeemError;

  @override
  Future<EllaEntitlement> fetch() async => fetchResult;

  @override
  Future<EllaEntitlement> redeem(String code) async {
    if (redeemError != null) throw EllaInviteRedemptionException(redeemError!);
    return redeemResult ?? EllaAccessDemoFixtures.active;
  }
}

class EllaAccessDemoFixtures {
  static final DateTime resetTomorrow = DateTime(2026, 7, 27, 0);
  static final DateTime resetNextMonth = DateTime(2026, 8, 1, 0);

  static EllaQuota quota({
    int dailyUsedSeconds = 8 * 60,
    int dailyLimitSeconds = 45 * 60,
    int monthlyUsedSeconds = 2 * 60 * 60,
    int monthlyLimitSeconds = 12 * 60 * 60,
    int maxSessionSeconds = 20 * 60,
    DateTime? resetsAt,
  }) =>
      EllaQuota(
        dailyUsedSeconds: dailyUsedSeconds,
        dailyLimitSeconds: dailyLimitSeconds,
        monthlyUsedSeconds: monthlyUsedSeconds,
        monthlyLimitSeconds: monthlyLimitSeconds,
        maxSessionSeconds: maxSessionSeconds,
        resetsAt: resetsAt ?? resetTomorrow,
      );

  static final EllaEntitlement none = EllaEntitlement(status: EllaEntitlementStatus.none, quota: quota());
  static final EllaEntitlement invited = EllaEntitlement(status: EllaEntitlementStatus.invited, quota: quota());
  static final EllaEntitlement active = EllaEntitlement(status: EllaEntitlementStatus.active, quota: quota());
  static final EllaEntitlement suspended = EllaEntitlement(status: EllaEntitlementStatus.suspended, quota: quota());
  static final EllaEntitlement revoked = EllaEntitlement(status: EllaEntitlementStatus.revoked, quota: quota());
  static final EllaEntitlement expired = EllaEntitlement(status: EllaEntitlementStatus.expired, quota: quota());
  static final EllaEntitlement softDaily = EllaEntitlement(
    status: EllaEntitlementStatus.active,
    quota: quota(dailyUsedSeconds: 37 * 60),
  );
  static final EllaEntitlement hardDaily = EllaEntitlement(
    status: EllaEntitlementStatus.active,
    quota: quota(dailyUsedSeconds: 45 * 60),
  );
  static final EllaEntitlement hardMonthly = EllaEntitlement(
    status: EllaEntitlementStatus.active,
    quota: quota(monthlyUsedSeconds: 12 * 60 * 60, resetsAt: resetNextMonth),
  );

  static final EllaEntitlementTransport defaultTransport = EllaAccessDemoTransport(
    fetchResult: invited,
    redeemResult: active,
  );
}
