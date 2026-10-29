import 'package:flutter/material.dart';
import 'package:package_info_plus/package_info_plus.dart';

/// Never display userinfo, paths, queries, fragments or invalid raw input.
String baseBackendOrigin(String? input) {
  try {
    if (input == null || input.trim().isEmpty) return 'Unavailable';
    final uri = Uri.tryParse(input.trim());
    if (uri == null || !{'http', 'https'}.contains(uri.scheme.toLowerCase()) || uri.host.isEmpty) {
      return 'Unavailable';
    }
    return Uri(scheme: uri.scheme.toLowerCase(), host: uri.host,
        port: uri.hasPort ? uri.port : null).toString();
  } catch (_) {
    return 'Unavailable';
  }
}

Future<PackageInfo> loadBaseInstalledVersion() => PackageInfo.fromPlatform();

String readBaseBackendOrigin(String? Function() read) {
  try { return baseBackendOrigin(read()); } catch (_) { return 'Unavailable'; }
}

Future<String> baseInstalledVersionText(Future<PackageInfo> Function() load) async {
  try {
    final info = await load();
    return info.version + ' (' + info.buildNumber + ')';
  } catch (_) {
    return 'Version unavailable';
  }
}

/// Presentation only: no login, capture, network or persistence effects.
class EllaBaseInfoBar extends StatelessWidget {
  const EllaBaseInfoBar({super.key, required this.readBackend,
    this.loadVersion = loadBaseInstalledVersion,
    this.upstreamCommit = const String.fromEnvironment('ELLA_BASE_UPSTREAM_COMMIT')});
  final String? Function() readBackend;
  final Future<PackageInfo> Function() loadVersion;
  final String upstreamCommit;

  Future<void> _open(BuildContext context) async {
    final origin = readBaseBackendOrigin(readBackend);
    final version = baseInstalledVersionText(loadVersion);
    final commit = RegExp(r'^[0-9a-fA-F]{40}$').hasMatch(upstreamCommit) ? upstreamCommit : null;
    await showDialog<void>(context: context, builder: (context) => AlertDialog(
      scrollable: true, title: const Text('Ella Base Info'),
      content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text('Installed version'),
          FutureBuilder<String>(future: version, builder: (context, snapshot) {
            if (snapshot.hasError) return const Text('Version unavailable');
            final display = snapshot.data;
            if (display == null) return const Text('Loading installed version…');
            return SelectableText(display);
          }),
          const SizedBox(height: 16), const Text('Backend origin'),
          SelectableText(origin, key: const ValueKey('ella-base-backend-origin')),
          const SizedBox(height: 4),
          const Text('Active configuration when opened. Paths and credentials are omitted.'),
          if (commit != null) ...[
            const SizedBox(height: 16), const Text('Upstream revision'), SelectableText(commit),
          ],
        ]),
      actions: [TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Close'))],
    ));
  }

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
    child: Align(alignment: Alignment.centerLeft,
      child: Wrap(spacing: 8, crossAxisAlignment: WrapCrossAlignment.center, children: [
        Text('Ella Base', style: Theme.of(context).textTheme.titleSmall),
        TextButton.icon(onPressed: () => _open(context),
          icon: const Icon(Icons.info_outline, size: 20), label: const Text('Info')),
      ])));
}
