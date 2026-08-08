import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';
import 'package:omi/providers/memories_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/people_provider.dart';
import 'package:omi/services/auth_service.dart';

/// Clears all user-specific data from local cache and in-memory providers,
/// then signs out of Firebase. Call this on every sign-out path to prevent
/// data leaking between accounts (see issue #300).
Future<void> signOutAndClearUserData(BuildContext context) async {
  CaptureProvider? captureProvider;
  ConversationProvider? conversationProvider;
  MessageProvider? messageProvider;
  PeopleProvider? peopleProvider;
  MemoriesProvider? memoriesProvider;
  EllaEntitlementProvider? entitlementProvider;
  try {
    captureProvider = context.read<CaptureProvider>();
  } catch (_) {}
  try {
    conversationProvider = context.read<ConversationProvider>();
  } catch (_) {}
  try {
    messageProvider = context.read<MessageProvider>();
  } catch (_) {}
  try {
    peopleProvider = context.read<PeopleProvider>();
  } catch (_) {}
  try {
    memoriesProvider = context.read<MemoriesProvider>();
  } catch (_) {}
  try {
    entitlementProvider = context.read<EllaEntitlementProvider>();
  } catch (_) {}
  await AuthService.instance.signOutWithQuiescedCleanup(() async {
    // Clear in-memory provider state only after every account producer has
    // quiesced. Firebase sign-out follows this callback in the same transition.
    try {
      conversationProvider?.reset();
    } catch (_) {}
    try {
      messageProvider?.reset();
    } catch (_) {}
    try {
      captureProvider?.reset();
    } catch (_) {}
    try {
      peopleProvider?.reset();
    } catch (_) {}
    try {
      memoriesProvider?.reset();
    } catch (_) {}
    try {
      entitlementProvider?.reset();
    } catch (_) {}

    // Explicitly clear conversation/message caches first (prevents cross-account data leak)
    SharedPreferencesUtil().clearUserCaches();

    // Clear all persisted local data before Firebase changes identity.
    await SharedPreferencesUtil().clear();
  });
}
