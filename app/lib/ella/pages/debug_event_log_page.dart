import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:omi/ella/services/debug_event_buffer.dart';

class DebugEventLogPage extends StatefulWidget {
  const DebugEventLogPage({super.key});

  @override
  State<DebugEventLogPage> createState() => _DebugEventLogPageState();
}

class _DebugEventLogPageState extends State<DebugEventLogPage> {
  final _buffer = DebugEventBuffer.instance;

  @override
  void initState() {
    super.initState();
    _buffer.addListener(_onUpdate);
    _buffer.refresh();
  }

  @override
  void dispose() {
    _buffer.removeListener(_onUpdate);
    super.dispose();
  }

  void _onUpdate() => setState(() {});

  @override
  Widget build(BuildContext context) {
    final events = _buffer.events;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Debug Event Log'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: _buffer.refresh,
          ),
          IconButton(
            icon: const Icon(Icons.delete_outline),
            onPressed: () async {
              await _buffer.clear();
            },
          ),
        ],
      ),
      body: events.isEmpty
          ? const Center(
              child: Text(
                'No debug events yet.\nWaiting for scanner activity...',
                textAlign: TextAlign.center,
              ),
            )
          : ListView.builder(
              itemCount: events.length,
              itemBuilder: (context, i) {
                final e = events[i];
                return ExpansionTile(
                  leading: Text(
                    _emoji(e.triggerType),
                    style: const TextStyle(fontSize: 20),
                  ),
                  title: Text(e.message, style: const TextStyle(fontSize: 13)),
                  subtitle: Text(
                    '${e.triggerType}  ·  ${_formatTime(e.receivedAt)}',
                    style: TextStyle(fontSize: 11, color: Colors.grey[600]),
                  ),
                  children: [
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
            ),
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
