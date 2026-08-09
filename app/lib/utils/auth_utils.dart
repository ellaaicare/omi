import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'package:omi/providers/capture_provider.dart';
import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/providers/ella_entitlement_provider.dart';
import 'package:omi/providers/memories_provider.dart';
import 'package:omi/providers/message_provider.dart';
import 'package:omi/providers/people_provider.dart';
import 'package:omi/services/auth_service.dart';

/// Resets mounted account providers before the structural AuthService logout.
/// AuthService owns the verified persisted-cache purge for every sign-out path.
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
    conversationProvider?.reset();
    messageProvider?.reset();
    captureProvider?.reset();
    peopleProvider?.reset();
    memoriesProvider?.reset();
    entitlementProvider?.reset();
  });
}
