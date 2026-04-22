import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:omi/ella/services/debug_event_buffer.dart';
import 'package:omi/ella/services/debug_metadata_api.dart';

class DebugEventLogPage extends StatefulWidget {
  const DebugEventLogPage({super.key});

  @override
  State<DebugEventLogPage> createState() => _DebugEventLogPageState();
}

class _DebugEventLogPageState extends State<DebugEventLogPage> {
  final _buffer = DebugEventBuffer.instance;
  List<DebugMetadataItem> _metadataItems = [];
  DebugMetadataItem? _selectedMetadata;
  bool _loadingMetadata = false;

  @override
  void initState() {
    super.initState();
    _buffer.addListener(_onUpdate);
    _buffer.refresh();
    _refreshMetadata();
  }

  @override
  void dispose() {
    _buffer.removeListener(_onUpdate);
    super.dispose();
  }

  void _onUpdate() => setState(() {});

  Future<void> _refreshAll() async {
    await Future.wait([_buffer.refresh(), _refreshMetadata()]);
  }

  Future<void> _refreshMetadata() async {
    setState(() => _loadingMetadata = true);
    final items = await fetchRecentDebugMetadata();
    if (!mounted) return;
    setState(() {
      _metadataItems = items;
      _selectedMetadata = items.isNotEmpty ? items.first : null;
      _loadingMetadata = false;
    });
  }

  Future<void> _loadMetadataForEvent(DebugEvent event) async {
    final conversationId = event.metadata['conversation_id'] as String? ?? '';
    if (conversationId.isEmpty) return;

    setState(() => _loadingMetadata = true);
    final item = await fetchDebugMetadata(conversationId);
    if (!mounted) return;
    setState(() {
      if (item != null) {
        _selectedMetadata = item;
        final withoutDuplicate = _metadataItems.where((m) => m.conversationId != item.conversationId).toList();
        _metadataItems = [item, ...withoutDuplicate];
      }
      _loadingMetadata = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    final events = _buffer.events;
    return DefaultTabController(
      length: 2,
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Debug Event Log'),
          bottom: const TabBar(
            tabs: [
              Tab(text: 'Live Events'),
              Tab(text: 'Metadata'),
            ],
          ),
          actions: [
            IconButton(icon: const Icon(Icons.refresh), onPressed: _refreshAll),
            IconButton(
              icon: const Icon(Icons.delete_outline),
              onPressed: () async {
                await _buffer.clear();
              },
            ),
          ],
        ),
        body: TabBarView(children: [_buildEventsTab(events), _buildMetadataTab()]),
      ),
    );
  }

  Widget _buildEventsTab(List<DebugEvent> events) {
    if (events.isEmpty) {
      return const Center(
        child: Text(
          'No live debug events yet.\nCheck Metadata for persisted Observer sidecars.',
          textAlign: TextAlign.center,
        ),
      );
    }

    return ListView.builder(
      itemCount: events.length,
      itemBuilder: (context, i) {
        final e = events[i];
        final conversationId = e.metadata['conversation_id'] as String? ?? '';
        return ExpansionTile(
          leading: Text(_emoji(e.triggerType), style: const TextStyle(fontSize: 20)),
          title: Text(e.message, style: const TextStyle(fontSize: 13)),
          subtitle: Text(
            '${e.triggerType}  ·  ${_formatTime(e.receivedAt)}',
            style: TextStyle(fontSize: 11, color: Colors.grey[600]),
          ),
          children: [
            if (conversationId.isNotEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    onPressed: _loadingMetadata ? null : () => _loadMetadataForEvent(e),
                    icon: const Icon(Icons.data_object, size: 16),
                    label: Text('Load sidecar metadata for $conversationId'),
                  ),
                ),
              ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: SelectableText(
                const JsonEncoder.withIndent('  ').convert(e.metadata),
                style: const TextStyle(fontFamily: 'monospace', fontSize: 11),
              ),
            ),
          ],
        );
      },
    );
  }

  Widget _buildMetadataTab() {
    if (_loadingMetadata && _metadataItems.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_metadataItems.isEmpty) {
      return const Center(child: Text('No Observer sidecar metadata found yet.', textAlign: TextAlign.center));
    }

    return Column(
      children: [
        Expanded(
          flex: 2,
          child: ListView.builder(
            itemCount: _metadataItems.length,
            itemBuilder: (context, i) {
              final item = _metadataItems[i];
              final selected = item.conversationId == _selectedMetadata?.conversationId;
              return ListTile(
                selected: selected,
                dense: true,
                leading: const Icon(Icons.data_object, size: 20),
                title: Text(item.conversationId, style: const TextStyle(fontSize: 13)),
                subtitle: Text(
                  '${item.path}  ·  ${item.lastModified != null ? _formatTime(item.lastModified!) : 'unknown'}',
                  style: TextStyle(fontSize: 11, color: Colors.grey[600]),
                ),
                onTap: () => setState(() => _selectedMetadata = item),
              );
            },
          ),
        ),
        const Divider(height: 1),
        Expanded(
          flex: 3,
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(16),
            child: SelectableText(
              const JsonEncoder.withIndent('  ').convert(_selectedMetadata?.metadata ?? {}),
              style: const TextStyle(fontFamily: 'monospace', fontSize: 11),
            ),
          ),
        ),
      ],
    );
  }

  String _emoji(String triggerType) {
    if (triggerType.contains('escalat')) return '🔴';
    if (triggerType.contains('clear')) return '🟢';
    if (triggerType.contains('filter')) return '🟡';
    if (triggerType.contains('prefix')) return '🔵';
    if (triggerType.contains('observer')) return '🧠';
    if (triggerType.contains('mode')) return '🔵';
    if (triggerType.contains('wakeword') || triggerType.contains('fastpath')) return '⚡';
    return '🔷';
  }

  String _formatTime(DateTime dt) {
    final local = dt.toLocal();
    return '${local.hour.toString().padLeft(2, '0')}:${local.minute.toString().padLeft(2, '0')}:${local.second.toString().padLeft(2, '0')}';
  }
}
