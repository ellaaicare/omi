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
  // Clear in-memory provider state
  try {
    context.read<ConversationProvider>().reset();
  } catch (_) {}
  try {
    context.read<MessageProvider>().reset();
  } catch (_) {}
  try {
    context.read<CaptureProvider>().reset();
  } catch (_) {}
  try {
    context.read<PeopleProvider>().reset();
  } catch (_) {}
  try {
    context.read<MemoriesProvider>().reset();
  } catch (_) {}
  try {
    context.read<EllaEntitlementProvider>().reset();
  } catch (_) {}

  // Explicitly clear conversation/message caches first (prevents cross-account data leak)
  SharedPreferencesUtil().clearUserCaches();

  // Clear all persisted local data
  await SharedPreferencesUtil().clear();

  // Sign out of Firebase
  await AuthService.instance.signOut();
}
