import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:record/record.dart';
import 'package:path_provider/path_provider.dart';
import 'package:path/path.dart' as p;

import '../network/chili_api_client.dart';

/// A hold-to-record microphone button.
///
/// While held, it records audio via the `record` package.
/// On release, the recording is sent to the backend for
/// speech-to-text and then processed as a chat message.
class VoiceInputButton extends StatefulWidget {
  /// Called with the transcribed text (or CHILI's reply) after recording.
  final ValueChanged<String> onResult;

  /// Called when recording starts/stops so the parent can update avatar state.
  final ValueChanged<bool> onRecordingStateChanged;

  const VoiceInputButton({
    super.key,
    required this.onResult,
    required this.onRecordingStateChanged,
  });

  @override
  State<VoiceInputButton> createState() => _VoiceInputButtonState();
}

class _VoiceInputButtonState extends State<VoiceInputButton> {
  final _recorder = AudioRecorder();
  bool _isRecording = false;
  String? _recordingPath;

  @override
  void dispose() {
    _recorder.dispose();
    super.dispose();
  }

  Future<void> _startRecording() async {
    if (_isRecording) return;

    final hasPermission = await _recorder.hasPermission();
    if (!hasPermission) {
      widget.onResult('Microphone permission denied.');
      return;
    }

    final dir = await getTemporaryDirectory();
    final filePath = p.join(dir.path, 'chili_voice_${DateTime.now().millisecondsSinceEpoch}.wav');

    await _recorder.start(
      const RecordConfig(encoder: AudioEncoder.wav),
      path: filePath,
    );

    setState(() {
      _isRecording = true;
      _recordingPath = filePath;
    });
    widget.onRecordingStateChanged(true);
  }

  Future<void> _stopRecording() async {
    if (!_isRecording) return;

    final path = await _recorder.stop();
    setState(() => _isRecording = false);
    widget.onRecordingStateChanged(false);

    if (path == null || path.isEmpty) {
      widget.onResult('Recording failed.');
      return;
    }

    // Send the audio file to the backend voice endpoint for STT + chat.
    try {
      final client = ChiliApiClient();
      final reply = await client.sendVoice(File(path));
      widget.onResult(reply);
    } catch (e) {
      widget.onResult('Voice processing failed: $e');
    } finally {
      // Clean up temp file.
      try {
        final f = File(path);
        if (await f.exists()) await f.delete();
      } catch (_) {}
    }
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onLongPressStart: (_) => _startRecording(),
      onLongPressEnd: (_) => _stopRecording(),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        width: 40,
        height: 40,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: _isRecording ? Colors.red : const Color(0xFFEF5350),
          boxShadow: _isRecording
              ? [BoxShadow(color: Colors.red.withOpacity(0.5), blurRadius: 12)]
              : [],
        ),
        child: Icon(
          _isRecording ? Icons.mic : Icons.mic_none,
          color: Colors.white,
          size: 22,
        ),
      ),
    );
  }
}
