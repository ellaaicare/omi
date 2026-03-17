import 'package:flutter/material.dart';

final List<Color> speakerColors = [
  Color(0xFFCDE8E3), // Light teal
  Color(0xFFCCDFF0), // Light blue
  Color(0xFFCFE8D5), // Light sage
  Color(0xFFF0DEC8), // Warm peach
  Color(0xFFDFD4F0), // Light lavender
  Color(0xFFF0EAC4), // Warm yellow
  Color(0xFFCFDBF0), // Light periwinkle
  Color(0xFFF0D0D0), // Light rose
];

final List<String> speakerImagePath = [
  'assets/images/speaker_1_icon.png',
  // 'assets/images/speaker_1_red.png',
  // 'assets/images/speaker_1_blue.png',
  // 'assets/images/speaker_1_green.png',
  // 'assets/images/speaker_1_yellow.png',
  // 'assets/images/speaker_1_purple.png',
  // 'assets/images/speaker_1_orange.png',
  // 'assets/images/speaker_1_pink.png',
  // 'assets/images/speaker_1_teal.png',
  // 'assets/images/speaker_1_cyan.png',
  // 'assets/images/speaker_1_amber.png',
];

class Person {
  final String id;
  final String name;
  final DateTime createdAt;
  final DateTime updatedAt;
  final List<String>? speechSamples;
  final List<String>? speechSampleTranscripts;
  final int speechSamplesVersion;
  final int? colorIdx;

  Person({
    required this.id,
    required this.name,
    required this.createdAt,
    required this.updatedAt,
    this.speechSamples,
    this.speechSampleTranscripts,
    this.speechSamplesVersion = 1,
    this.colorIdx,
  });

  factory Person.fromJson(Map<String, dynamic> json) {
    return Person(
      id: json['id'],
      name: json['name'],
      createdAt: DateTime.parse(json['created_at']).toLocal(),
      updatedAt: DateTime.parse(json['updated_at']).toLocal(),
      speechSamples: json['speech_samples'] != null ? List<String>.from(json['speech_samples']) : [],
      speechSampleTranscripts: json['speech_sample_transcripts'] != null
          ? List<String>.from(json['speech_sample_transcripts'])
          : null,
      speechSamplesVersion: json['speech_samples_version'] ?? 1,
      colorIdx: json['color_idx'] ?? json['id'].hashCode % speakerColors.length,
    );
  }

  Map<String, dynamic> toJson() {
    return {
      'id': id,
      'name': name,
      'created_at': createdAt.toUtc().toIso8601String(),
      'updated_at': updatedAt.toUtc().toIso8601String(),
      'speech_samples': speechSamples ?? [],
      'speech_sample_transcripts': speechSampleTranscripts,
      'speech_samples_version': speechSamplesVersion,
      'color_idx': colorIdx,
    };
  }
}
