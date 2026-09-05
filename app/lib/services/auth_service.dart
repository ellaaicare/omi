import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'package:app_links/app_links.dart';
import 'package:crypto/crypto.dart';
import 'package:firebase_auth/firebase_auth.dart';
import 'package:google_sign_in/google_sign_in.dart';
import 'package:http/http.dart' as http;
import 'package:sign_in_with_apple/sign_in_with_apple.dart';
import 'package:url_launcher/url_launcher.dart';

import 'package:omi/backend/http/api/users.dart';
import 'package:omi/backend/preferences.dart';
import 'package:omi/env/env.dart';
import 'package:omi/ella/services/ella_account_isolation_service.dart';
import 'package:omi/ella/services/diagnostics/ella_diagnostic_event_journal.dart';
import 'package:omi/utils/logger.dart';
import 'package:omi/utils/platform/platform_service.dart';

/// Detects if the app is running in an iOS simulator
/// Used for automated testing to bypass authentication
bool isSimulator() {
  if (!Platform.isIOS) return false;

  // Check for simulator environment variables
  return Platform.environment['SIMULATOR_DEVICE_NAME'] != null ||
      Platform.environment['SIMULATOR_MODEL_IDENTIFIER'] != null;
}

class AuthService {
  static final AuthService _instance = AuthService._internal();
  static AuthService get instance => _instance;

  AuthService._internal();

  Future<T> runIdentityTransition<T>(Future<T> Function() mutation) async {
    await const EllaAccountIsolationService().stopForAccountTransition();
    await EllaDiagnosticEventJournal.instance.clearAll();
    return mutation();
  }

  Future<UserCredential> replaceIdentityWithCredential(AuthCredential credential) => runIdentityTransition(() async {
        await FirebaseAuth.instance.signOut();
        return FirebaseAuth.instance.signInWithCredential(credential);
      });

  bool isSignedIn() => FirebaseAuth.instance.currentUser != null && !FirebaseAuth.instance.currentUser!.isAnonymous;

  getFirebaseUser() {
    return FirebaseAuth.instance.currentUser;
  }

  /// Google Sign In using the standard google_sign_in package (iOS, Android)
  Future<UserCredential?> signInWithGoogleMobile() async {
    // Trigger the authentication flow
    final GoogleSignInAccount? googleUser = await GoogleSignIn(scopes: ['profile', 'email']).signIn();
    // Obtain the auth details from the request
    final GoogleSignInAuthentication? googleAuth = await googleUser?.authentication;
    if (googleAuth == null) {
      Logger.error('An error occurred while signing in. Please try again later. (Error: 40001)');
      return null;
    }

    // Create a new credential
    if (googleAuth.accessToken == null && googleAuth.idToken == null) {
      Logger.error('An error occurred while signing in. Please try again later. (Error: 40002)');
      return null;
    }
    final credential = GoogleAuthProvider.credential(accessToken: googleAuth.accessToken, idToken: googleAuth.idToken);

    // Once signed in, return the UserCredential
    try {
      var result = await runIdentityTransition(() => FirebaseAuth.instance.signInWithCredential(credential));
      await _updateUserPreferences(result, 'google');
      return result;
    } catch (_) {
      rethrow;
    }
  }

  /// Generates a cryptographically secure random nonce, to be included in a
  /// credential request.
  String generateNonce([int length = 32]) {
    const charset = '0123456789ABCDEFGHIJKLMNOPQRSTUVXYZabcdefghijklmnopqrstuvwxyz-._';
    final random = Random.secure();
    return List.generate(length, (_) => charset[random.nextInt(charset.length)]).join();
  }

  /// Returns the sha256 hash of [input] in hex notation.
  String sha256ofString(String input) {
    final bytes = utf8.encode(input);
    final digest = sha256.convert(bytes);
    return digest.toString();
  }

  Future<UserCredential?> signInWithAppleMobile() async {
    try {
      final rawNonce = generateNonce();
      final nonce = sha256ofString(rawNonce);

      final appleCredential = await SignInWithApple.getAppleIDCredential(
        scopes: [AppleIDAuthorizationScopes.email, AppleIDAuthorizationScopes.fullName],
        nonce: nonce,
      );

      if (appleCredential.identityToken == null) {
        throw Exception('Apple Sign In failed - no identity token received.');
      }

      // Create an `OAuthCredential` from the credential returned by Apple.
      final oauthCredential = OAuthProvider("apple.com").credential(
        idToken: appleCredential.identityToken,
        rawNonce: rawNonce,
        accessToken: appleCredential.authorizationCode,
      );

      // Sign in the user with Firebase.
      UserCredential userCred = await replaceIdentityWithCredential(oauthCredential);

      // Extract name from Apple credential (only available on first sign-in)
      if (appleCredential.givenName != null && appleCredential.givenName!.isNotEmpty) {
        SharedPreferencesUtil().givenName = appleCredential.givenName!;
        if (appleCredential.familyName != null && appleCredential.familyName!.isNotEmpty) {
          SharedPreferencesUtil().familyName = appleCredential.familyName!;
        }

        // Update Firebase profile with the name
        final fullName = appleCredential.familyName != null && appleCredential.familyName!.isNotEmpty
            ? '${appleCredential.givenName} ${appleCredential.familyName}'
            : appleCredential.givenName!;
        try {
          await userCred.user?.updateProfile(displayName: fullName);
          await userCred.user?.reload();
        } catch (_) {}
      }

      await _updateUserPreferences(userCred, 'apple');

      return userCred;
    } on FirebaseAuthException {
      return null;
    } catch (e) {
      Logger.handle(e, null, message: 'An error occurred while signing in. Please try again later.');
      return null;
    }
  }

  Future<void> signInAnonymously() async {
    try {
      await runIdentityTransition(FirebaseAuth.instance.signInAnonymously);
      var user = FirebaseAuth.instance.currentUser!;
      SharedPreferencesUtil().uid = user.uid;
      await getIdToken();
    } catch (e) {
      Logger.handle(e, null, message: 'An error occurred while signing in. Please try again later.');
    }
  }

  /// Quiesce account-scoped producers once, run the caller's local cleanup,
  /// then mutate Firebase identity. Cleanup remains inside the transition so
  /// caches cannot survive into the next account, while callers that already
  /// need cleanup do not invoke the full shutdown sequence a second time.
  Future<void> signOutWithQuiescedCleanup(Future<void> Function() cleanup) => runIdentityTransition(() async {
        await cleanup();
        await FirebaseAuth.instance.signOut();
      });

  Future<void> signOut() => signOutWithQuiescedCleanup(() async {});

  Future<String?> getIdToken() async {
    try {
      IdTokenResult? newToken = await FirebaseAuth.instance.currentUser?.getIdTokenResult(true);
      if (newToken?.token != null) {
        var user = FirebaseAuth.instance.currentUser!;
        SharedPreferencesUtil().uid = user.uid;
        SharedPreferencesUtil().tokenExpirationTime = newToken?.expirationTime?.millisecondsSinceEpoch ?? 0;
        SharedPreferencesUtil().authToken = newToken?.token ?? '';
        if (SharedPreferencesUtil().email.isEmpty) {
          SharedPreferencesUtil().email = user.email ?? '';
        }

        if (SharedPreferencesUtil().givenName.isEmpty) {
          SharedPreferencesUtil().givenName = user.displayName?.split(' ')[0] ?? '';
          if ((user.displayName?.split(' ').length ?? 0) > 1) {
            SharedPreferencesUtil().familyName = user.displayName?.split(' ')[1] ?? '';
          } else {
            SharedPreferencesUtil().familyName = '';
          }
        }
      }
      return newToken?.token;
    } catch (_) {
      return SharedPreferencesUtil().authToken;
    }
  }

  // Method channel for direct deep link delivery (fallback for app_links)
  static const _deepLinkChannel = MethodChannel('com.omi/deep_links');

  Future<UserCredential?> authenticateWithProvider(String provider) async {
    try {
      final state = _generateState();
      const redirectUri = 'omi://auth/callback';

      final authUrl = '${Env.apiBaseUrl}v1/auth/authorize'
          '?provider=$provider'
          '&redirect_uri=${Uri.encodeComponent(redirectUri)}'
          '&state=$state';

      // Set up listeners before launching URL
      final appLinks = AppLinks();
      late StreamSubscription linkSubscription;
      final completer = Completer<String>();

      // Listen via app_links
      linkSubscription = appLinks.uriLinkStream.listen(
        (Uri uri) {
          if (uri.scheme == 'omi' && uri.host == 'auth' && uri.path == '/callback') {
            if (!completer.isCompleted) {
              linkSubscription.cancel();
              completer.complete(uri.toString());
            }
          }
        },
        onError: (error) {
          if (!completer.isCompleted) {
            linkSubscription.cancel();
            completer.completeError(error);
          }
        },
      );

      // Also listen via direct method channel (fallback)
      _deepLinkChannel.setMethodCallHandler((call) async {
        if (call.method == 'onDeepLink') {
          final urlString = call.arguments as String;
          final uri = Uri.parse(urlString);
          if (uri.scheme == 'omi' && uri.host == 'auth' && uri.path == '/callback') {
            if (!completer.isCompleted) {
              linkSubscription.cancel();
              _deepLinkChannel.setMethodCallHandler(null);
              completer.complete(urlString);
            }
          }
        }
      });

      // Now launch the URL
      final launched = await launchUrl(Uri.parse(authUrl), mode: LaunchMode.externalApplication);

      if (!launched) {
        linkSubscription.cancel();
        _deepLinkChannel.setMethodCallHandler(null);
        throw Exception('Failed to launch authentication URL');
      }

      final result = await completer.future.timeout(
        const Duration(minutes: 5),
        onTimeout: () {
          linkSubscription.cancel();
          _deepLinkChannel.setMethodCallHandler(null);
          throw Exception('Authentication timeout');
        },
      );

      final uri = Uri.parse(result);
      final code = uri.queryParameters['code'];
      final returnedState = uri.queryParameters['state'];

      if (code == null) {
        throw Exception('No authorization code received');
      }

      if (returnedState != state) {
        throw Exception('Invalid state parameter');
      }

      // Exchange the code for OAuth credentials
      final oauthCredentials = await _exchangeCodeForOAuthCredentials(code, redirectUri);

      if (oauthCredentials == null) {
        throw Exception('Failed to exchange code for OAuth credentials');
      }

      // Sign in to Firebase with the OAuth credentials
      final credential = await _signInWithOAuthCredentials(oauthCredentials);

      // Update user profile and local storage after successful sign-in
      await _updateUserPreferences(credential, provider);

      return credential;
    } catch (e) {
      Logger.handle(e, StackTrace.current, message: 'Authentication failed');
      return null;
    }
  }

  Future<Map<String, dynamic>?> _exchangeCodeForOAuthCredentials(String code, String redirectUri) async {
    try {
      final useCustomToken = Env.useAuthCustomToken;

      final response = await http.post(
        Uri.parse('${Env.apiBaseUrl}v1/auth/token'),
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: {
          'grant_type': 'authorization_code',
          'code': code,
          'redirect_uri': redirectUri,
          'use_custom_token': useCustomToken.toString(),
        },
      );

      if (response.statusCode == 200) {
        return json.decode(response.body);
      } else {
        return null;
      }
    } catch (_) {
      return null;
    }
  }

  Future<UserCredential> _signInWithOAuthCredentials(Map<String, dynamic> oauthCredentials) async {
    final provider = oauthCredentials['provider'];
    final useCustomToken = Env.useAuthCustomToken;
    final customToken = oauthCredentials['custom_token'];

    // Use custom token if enabled and available
    if (useCustomToken && customToken != null) {
      return runIdentityTransition(() => FirebaseAuth.instance.signInWithCustomToken(customToken));
    }

    // Fallback to OAuth credentials
    final idToken = oauthCredentials['id_token'];
    final accessToken = oauthCredentials['access_token'];

    if (provider == 'google') {
      final credential = GoogleAuthProvider.credential(idToken: idToken, accessToken: accessToken);
      return runIdentityTransition(() => FirebaseAuth.instance.signInWithCredential(credential));
    } else if (provider == 'apple') {
      final credential = OAuthProvider('apple.com').credential(idToken: idToken, accessToken: accessToken);
      return runIdentityTransition(() => FirebaseAuth.instance.signInWithCredential(credential));
    } else {
      throw Exception('Unsupported provider: $provider');
    }
  }

  Future<void> _updateUserPreferences(UserCredential result, String provider) async {
    try {
      final user = result.user;
      if (user == null) return;

      // Update UID and basic user info
      SharedPreferencesUtil().uid = user.uid;

      // Get user info from Firebase user and additional user info
      var email = user.email ?? '';
      var displayName = user.displayName ?? '';
      var givenName = '';
      var familyName = '';

      if (result.additionalUserInfo?.profile != null) {
        final profile = result.additionalUserInfo!.profile!;

        if (provider == 'google') {
          givenName = profile['given_name'] ?? '';
          familyName = profile['family_name'] ?? '';
          email = profile['email'] ?? email;
        } else if (provider == 'apple') {
          if (profile.containsKey('name')) {
            final name = profile['name'];
            if (name is Map) {
              givenName = name['firstName'] ?? '';
              familyName = name['lastName'] ?? '';
            }
          }
          email = profile['email'] ?? email;
        }
      }

      if (givenName.isEmpty && displayName.isNotEmpty) {
        var nameParts = displayName.split(' ');
        givenName = nameParts.isNotEmpty ? nameParts[0] : '';
        familyName = nameParts.length > 1 ? nameParts.sublist(1).join(' ') : '';
      }

      // Update SharedPreferences
      if (email.isNotEmpty) {
        SharedPreferencesUtil().email = email;
      }
      if (givenName.isNotEmpty) {
        SharedPreferencesUtil().givenName = givenName;
        SharedPreferencesUtil().familyName = familyName;
      }

      // Update Firebase user profile if needed
      if (displayName.isEmpty && givenName.isNotEmpty) {
        final fullName = familyName.isNotEmpty ? '$givenName $familyName' : givenName;
        try {
          await user.updateProfile(displayName: fullName);
          await user.reload();
        } catch (_) {}
      }

      // Restore onboarding state from server
      await _restoreOnboardingState();
    } catch (_) {}
  }

  /// Public wrapper for _restoreOnboardingState — used by MobileApp self-healing.
  Future<void> restoreOnboardingState() => _restoreOnboardingState();

  Future<void> _restoreOnboardingState() async {
    try {
      final state = await getUserOnboardingState();
      if (state != null) {
        if (state['completed'] == true) {
          SharedPreferencesUtil().onboardingCompleted = true;
        }
        final acquisitionSource = state['acquisition_source'] as String? ?? '';
        if (acquisitionSource.isNotEmpty) {
          SharedPreferencesUtil().foundOmiSource = acquisitionSource;
        }
        // Restore language from server if not already set locally
        final serverLanguage = await getUserPrimaryLanguage();
        if (serverLanguage != null && serverLanguage.isNotEmpty) {
          SharedPreferencesUtil().userPrimaryLanguage = serverLanguage;
          SharedPreferencesUtil().hasSetPrimaryLanguage = true;
        }
      }
    } catch (_) {}
  }

  Future<void> updateGivenName(String fullName) async {
    try {
      var user = FirebaseAuth.instance.currentUser;

      SharedPreferencesUtil().givenName = fullName.split(' ')[0];
      if (fullName.split(' ').length > 1) {
        SharedPreferencesUtil().familyName = fullName.split(' ').sublist(1).join(' ');
      }

      if (user == null) {
        return;
      }

      // Try to update Firebase profile with platform-specific handling
      // Skip Firebase updateProfile on Windows due to known crashes and threading issues
      // https://github.com/firebase/flutterfire/issues/13340
      // https://github.com/firebase/flutterfire/issues/12725
      if (!PlatformService.isWindows) {
        try {
          // Web and other desktop platforms may still have issues, so use timeout
          if (kIsWeb || PlatformService.isDesktop) {
            // Try with a timeout to prevent hanging
            await user.updateProfile(displayName: fullName).timeout(
              const Duration(seconds: 5),
              onTimeout: () {
                throw TimeoutException('updateProfile timed out', const Duration(seconds: 5));
              },
            );
          } else {
            await user.updateProfile(displayName: fullName);
          }
          await user.reload();
          user = FirebaseAuth.instance.currentUser;
        } catch (_) {}
      }
    } catch (_) {
      // Ensure SharedPreferences are updated even if everything else fails
      try {
        SharedPreferencesUtil().givenName = fullName.split(' ')[0];
        if (fullName.split(' ').length > 1) {
          SharedPreferencesUtil().familyName = fullName.split(' ').sublist(1).join(' ');
        }
      } catch (_) {}
    }
  }

  String _generateState() {
    final random = Random.secure();
    final bytes = Uint8List(32);
    for (int i = 0; i < 32; i++) {
      bytes[i] = random.nextInt(256);
    }
    return base64Url.encode(bytes);
  }

  Future<UserCredential?> linkWithProvider(String provider) async {
    try {
      final currentUser = FirebaseAuth.instance.currentUser;
      if (currentUser == null) {
        throw Exception('No user is currently signed in');
      }

      final state = _generateState();
      const redirectUri = 'omi://auth/callback';

      final authUrl = '${Env.apiBaseUrl}v1/auth/authorize'
          '?provider=$provider'
          '&redirect_uri=${Uri.encodeComponent(redirectUri)}'
          '&state=$state';

      final launched = await launchUrl(Uri.parse(authUrl), mode: LaunchMode.externalApplication);

      if (!launched) {
        throw Exception('Failed to launch authentication URL');
      }

      // Listen for the callback URL using app_links
      final appLinks = AppLinks();
      late StreamSubscription linkSubscription;
      final completer = Completer<String>();

      linkSubscription = appLinks.uriLinkStream.listen(
        (Uri uri) {
          if (uri.scheme == 'omi' && uri.host == 'auth' && uri.path == '/callback') {
            linkSubscription.cancel();
            completer.complete(uri.toString());
          }
        },
        onError: (error) {
          linkSubscription.cancel();
          completer.completeError(error);
        },
      );

      final result = await completer.future.timeout(
        const Duration(minutes: 5),
        onTimeout: () {
          linkSubscription.cancel();
          throw Exception('Authentication timeout');
        },
      );

      final uri = Uri.parse(result);
      final code = uri.queryParameters['code'];
      final returnedState = uri.queryParameters['state'];

      if (code == null) {
        throw Exception('No authorization code received');
      }

      if (returnedState != state) {
        throw Exception('Invalid state parameter');
      }

      // Exchange the code for OAuth credentials
      final oauthCredentials = await _exchangeCodeForOAuthCredentials(code, redirectUri);

      if (oauthCredentials == null) {
        throw Exception('Failed to exchange code for OAuth credentials');
      }

      // Create Firebase credential
      final credential = await _createFirebaseCredential(oauthCredentials);

      try {
        // Link the credential to the current user
        final result = await currentUser.linkWithCredential(credential);

        // Update user preferences after successful linking
        await _updateUserPreferences(result, provider);

        return result;
      } catch (e) {
        if (e is FirebaseAuthException && e.code == 'credential-already-in-use') {
          // Handle existing credential case
          return await _handleExistingCredential(e);
        }
        rethrow;
      }
    } catch (e) {
      Logger.handle(e, StackTrace.current, message: 'Account linking failed');
      rethrow;
    }
  }

  Future<AuthCredential> _createFirebaseCredential(Map<String, dynamic> oauthCredentials) async {
    final provider = oauthCredentials['provider'];
    final idToken = oauthCredentials['id_token'];
    final accessToken = oauthCredentials['access_token'];

    if (provider == 'google') {
      return GoogleAuthProvider.credential(idToken: idToken, accessToken: accessToken);
    } else if (provider == 'apple') {
      return OAuthProvider('apple.com').credential(idToken: idToken, accessToken: accessToken);
    } else {
      throw Exception('Unsupported provider: $provider');
    }
  }

  /// Handle the case when credential is already in use
  Future<UserCredential?> _handleExistingCredential(FirebaseAuthException e) async {
    // Get existing user credentials
    final existingCred = e.credential;

    final result = await replaceIdentityWithCredential(existingCred!);
    final newUserId = FirebaseAuth.instance.currentUser?.uid;
    await getIdToken();

    // Restore onboarding state from server instead of resetting to false.
    // Setting onboardingCompleted=false here was causing existing users to
    // see the new-user flow after re-authentication (issue #633).
    await restoreOnboardingState();
    SharedPreferencesUtil().uid = newUserId ?? '';
    SharedPreferencesUtil().email = FirebaseAuth.instance.currentUser?.email ?? '';
    SharedPreferencesUtil().givenName = FirebaseAuth.instance.currentUser?.displayName?.split(' ')[0] ?? '';

    return result;
  }

  Future<UserCredential?> linkWithGoogle() async {
    return await linkWithProvider('google');
  }

  Future<UserCredential?> linkWithApple() async {
    return await linkWithProvider('apple');
  }
}
