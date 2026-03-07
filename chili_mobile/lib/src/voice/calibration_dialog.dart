import 'dart:async';

import 'package:flutter/material.dart';

import '../config/app_config.dart';
import 'wake_word_calibrator.dart';

/// Full-screen dialog that walks the user through wake-word calibration:
///   Phase 1 — Ambient noise measurement (3 s silence)
///   Phase 2 — Record 5 wake-word samples
///   Phase 3 — Verification sample
///
/// Returns `true` from [Navigator.pop] when calibration completes
/// successfully, `false` (or null) when skipped / cancelled.
class CalibrationDialog extends StatefulWidget {
  const CalibrationDialog({
    super.key,
    required this.wakeWord,
    this.pauseListening,
  });

  final String wakeWord;

  /// Set to `true` while calibrating so the main [WakeWordListener] yields
  /// the mic.
  final ValueNotifier<bool>? pauseListening;

  /// Show the dialog and return the result.
  static Future<bool> show(
    BuildContext context, {
    required String wakeWord,
    ValueNotifier<bool>? pauseListening,
  }) async {
    final result = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (_) => CalibrationDialog(
        wakeWord: wakeWord,
        pauseListening: pauseListening,
      ),
    );
    return result ?? false;
  }

  @override
  State<CalibrationDialog> createState() => _CalibrationDialogState();
}

enum _Phase { intro, ambient, sampling, verifying, done }

class _CalibrationDialogState extends State<CalibrationDialog>
    with SingleTickerProviderStateMixin {
  late final WakeWordCalibrator _calibrator;
  late final AnimationController _pulseCtrl;

  _Phase _phase = _Phase.intro;
  String _statusText = '';
  String _partialText = '';

  // Ambient
  double _ambientRms = 200.0;

  // Samples
  static const _totalSamples = 5;
  int _currentSample = 0;
  final List<String> _sampleResults = [];
  final Set<String> _learnedVariants = {};

  // Verification
  bool _verificationPassed = false;

  @override
  void initState() {
    super.initState();
    _calibrator = WakeWordCalibrator(
      onStatus: (s) {
        if (mounted) setState(() => _statusText = s);
      },
    );
    _pulseCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1200),
    )..repeat(reverse: true);
  }

  @override
  void dispose() {
    _calibrator.dispose();
    _pulseCtrl.dispose();
    widget.pauseListening?.value = false;
    super.dispose();
  }

  // ── Phase runners ────────────────────────────────────────────────────

  Future<void> _startCalibration() async {
    widget.pauseListening?.value = true;

    // Phase 1: ambient noise
    setState(() {
      _phase = _Phase.ambient;
      _statusText = 'Stay quiet for 3 seconds...';
    });

    try {
      _ambientRms = await _calibrator.calibrateAmbient(seconds: 3);
    } catch (e) {
      debugPrint('[CalibrationDialog] ambient error: $e');
    }

    if (!mounted) return;

    // Phase 2: samples
    setState(() {
      _phase = _Phase.sampling;
      _currentSample = 0;
      _statusText = '';
    });

    for (var i = 0; i < _totalSamples; i++) {
      if (!mounted) return;
      setState(() {
        _currentSample = i + 1;
        _partialText = '';
        _statusText = 'Say "${widget.wakeWord}" now... ($_currentSample/$_totalSamples)';
      });

      // Brief pause between samples.
      await Future<void>.delayed(const Duration(milliseconds: 600));

      final text = await _calibrator.recordSample(
        timeoutSeconds: 5,
        onPartial: (p) {
          if (mounted) setState(() => _partialText = p);
        },
      );

      if (!mounted) return;

      _sampleResults.add(text);
      if (text.isNotEmpty) {
        final normalized = text.trim().toLowerCase();
        final wakeLower = widget.wakeWord.toLowerCase();
        if (normalized != wakeLower) {
          _learnedVariants.add(normalized);
        }
      }

      setState(() => _partialText = '');
    }

    // Phase 3: verification
    setState(() {
      _phase = _Phase.verifying;
      _partialText = '';
      _statusText = 'One more time to verify...';
    });

    await Future<void>.delayed(const Duration(milliseconds: 600));

    final verifyText = await _calibrator.recordSample(
      timeoutSeconds: 5,
      onPartial: (p) {
        if (mounted) setState(() => _partialText = p);
      },
    );

    if (!mounted) return;

    final config = AppConfig.instance;
    // Temporarily add learned variants so isWakeWordMatch can check them.
    final allVariants = _learnedVariants.toList();
    await config.setCalibratedVariants(allVariants);

    _verificationPassed = verifyText.isNotEmpty &&
        (config.isWakeWordMatch(verifyText) ||
            allVariants.contains(verifyText.trim().toLowerCase()));

    // If the verification itself produced a new variant, learn it too.
    if (verifyText.isNotEmpty) {
      final vNorm = verifyText.trim().toLowerCase();
      if (vNorm != widget.wakeWord.toLowerCase() &&
          !_learnedVariants.contains(vNorm)) {
        _learnedVariants.add(vNorm);
      }
    }

    // Persist everything.
    await config.setCalibratedVariants(_learnedVariants.toList());
    await config.setAmbientRms(_ambientRms);
    await config.setCalibrationDone(true);

    widget.pauseListening?.value = false;

    setState(() {
      _phase = _Phase.done;
      _statusText = '';
    });
  }

  void _skip() {
    widget.pauseListening?.value = false;
    Navigator.of(context).pop(false);
  }

  void _finish() {
    Navigator.of(context).pop(true);
  }

  // ── UI ───────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Dialog(
      insetPadding: const EdgeInsets.all(24),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 440),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              _buildHeader(theme),
              const SizedBox(height: 20),
              _buildBody(theme),
              const SizedBox(height: 24),
              _buildActions(theme),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildHeader(ThemeData theme) {
    return Row(
      children: [
        Icon(Icons.mic, color: theme.colorScheme.primary, size: 28),
        const SizedBox(width: 12),
        Expanded(
          child: Text(
            'Wake Word Calibration',
            style: theme.textTheme.titleLarge,
          ),
        ),
      ],
    );
  }

  Widget _buildBody(ThemeData theme) {
    switch (_phase) {
      case _Phase.intro:
        return _buildIntro(theme);
      case _Phase.ambient:
        return _buildAmbient(theme);
      case _Phase.sampling:
        return _buildSampling(theme);
      case _Phase.verifying:
        return _buildVerifying(theme);
      case _Phase.done:
        return _buildDone(theme);
    }
  }

  Widget _buildIntro(ThemeData theme) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          'Let\'s teach CHILI to recognize your voice saying '
          '"${widget.wakeWord}".',
          style: theme.textTheme.bodyLarge,
          textAlign: TextAlign.center,
        ),
        const SizedBox(height: 12),
        Text(
          'We\'ll measure your room\'s background noise, then ask you to say '
          'the wake word 5 times so CHILI can learn how your mic picks it up.',
          style: theme.textTheme.bodyMedium?.copyWith(
            color: Colors.grey.shade600,
          ),
          textAlign: TextAlign.center,
        ),
      ],
    );
  }

  Widget _buildAmbient(ThemeData theme) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        _buildStepIndicator(0),
        const SizedBox(height: 16),
        AnimatedBuilder(
          animation: _pulseCtrl,
          builder: (_, __) => Icon(
            Icons.volume_off,
            size: 48 + _pulseCtrl.value * 8,
            color: Colors.amber.shade700,
          ),
        ),
        const SizedBox(height: 12),
        Text(
          'Stay quiet for a few seconds...',
          style: theme.textTheme.bodyLarge,
          textAlign: TextAlign.center,
        ),
        const SizedBox(height: 8),
        const LinearProgressIndicator(),
        if (_statusText.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            _statusText,
            style: theme.textTheme.bodySmall?.copyWith(
              color: Colors.grey.shade500,
            ),
          ),
        ],
      ],
    );
  }

  Widget _buildSampling(ThemeData theme) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        _buildStepIndicator(1),
        const SizedBox(height: 16),
        AnimatedBuilder(
          animation: _pulseCtrl,
          builder: (_, __) => Icon(
            Icons.mic,
            size: 48 + _pulseCtrl.value * 8,
            color: Colors.red.shade400,
          ),
        ),
        const SizedBox(height: 12),
        Text(
          'Say "${widget.wakeWord}" now',
          style: theme.textTheme.headlineSmall?.copyWith(
            fontWeight: FontWeight.bold,
          ),
          textAlign: TextAlign.center,
        ),
        const SizedBox(height: 8),
        LinearProgressIndicator(
          value: _currentSample / _totalSamples,
        ),
        const SizedBox(height: 4),
        Text(
          'Sample $_currentSample of $_totalSamples',
          style: theme.textTheme.bodySmall,
        ),
        if (_partialText.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            'Hearing: "$_partialText"',
            style: theme.textTheme.bodyMedium?.copyWith(
              fontStyle: FontStyle.italic,
              color: Colors.grey.shade600,
            ),
          ),
        ],
        if (_sampleResults.isNotEmpty) ...[
          const SizedBox(height: 12),
          Wrap(
            spacing: 6,
            runSpacing: 4,
            children: [
              for (final r in _sampleResults)
                _sampleChip(r, theme),
            ],
          ),
        ],
      ],
    );
  }

  Widget _buildVerifying(ThemeData theme) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        _buildStepIndicator(2),
        const SizedBox(height: 16),
        AnimatedBuilder(
          animation: _pulseCtrl,
          builder: (_, __) => Icon(
            Icons.verified_user,
            size: 48 + _pulseCtrl.value * 8,
            color: Colors.blue.shade400,
          ),
        ),
        const SizedBox(height: 12),
        Text(
          'Say "${widget.wakeWord}" one more time to verify',
          style: theme.textTheme.bodyLarge,
          textAlign: TextAlign.center,
        ),
        if (_partialText.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(
            'Hearing: "$_partialText"',
            style: theme.textTheme.bodyMedium?.copyWith(
              fontStyle: FontStyle.italic,
              color: Colors.grey.shade600,
            ),
          ),
        ],
      ],
    );
  }

  Widget _buildDone(ThemeData theme) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(
          _verificationPassed ? Icons.check_circle : Icons.warning_amber,
          size: 56,
          color: _verificationPassed ? Colors.green : Colors.orange,
        ),
        const SizedBox(height: 12),
        Text(
          _verificationPassed
              ? 'Calibration complete!'
              : 'Calibration saved (verification was uncertain)',
          style: theme.textTheme.titleMedium,
          textAlign: TextAlign.center,
        ),
        const SizedBox(height: 12),
        if (_learnedVariants.isNotEmpty) ...[
          Text(
            'Learned ${_learnedVariants.length} variant${_learnedVariants.length == 1 ? '' : 's'}:',
            style: theme.textTheme.bodyMedium,
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 4,
            children: [
              for (final v in _learnedVariants)
                Chip(
                  label: Text(v, style: const TextStyle(fontSize: 12)),
                  backgroundColor: Colors.green.shade50,
                  side: BorderSide(color: Colors.green.shade200),
                ),
            ],
          ),
        ] else
          Text(
            'No new variants needed — your pronunciation matched perfectly.',
            style: theme.textTheme.bodyMedium?.copyWith(
              color: Colors.grey.shade600,
            ),
            textAlign: TextAlign.center,
          ),
        const SizedBox(height: 8),
        Text(
          'Ambient noise: ${_ambientRms.toStringAsFixed(0)} RMS',
          style: theme.textTheme.bodySmall?.copyWith(
            color: Colors.grey.shade500,
          ),
        ),
      ],
    );
  }

  Widget _sampleChip(String text, ThemeData theme) {
    final isExact =
        text.trim().toLowerCase() == widget.wakeWord.toLowerCase();
    return Chip(
      avatar: Icon(
        isExact ? Icons.check : Icons.lightbulb_outline,
        size: 16,
        color: isExact ? Colors.green : Colors.orange.shade700,
      ),
      label: Text(
        text.isEmpty ? '(silence)' : text,
        style: const TextStyle(fontSize: 12),
      ),
      backgroundColor:
          isExact ? Colors.green.shade50 : Colors.orange.shade50,
      side: BorderSide(
        color: isExact ? Colors.green.shade200 : Colors.orange.shade200,
      ),
    );
  }

  Widget _buildStepIndicator(int activeStep) {
    const labels = ['Ambient', 'Samples', 'Verify'];
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        for (var i = 0; i < labels.length; i++) ...[
          if (i > 0)
            Container(
              width: 24,
              height: 2,
              color: i <= activeStep
                  ? Theme.of(context).colorScheme.primary
                  : Colors.grey.shade300,
            ),
          _StepDot(
            label: labels[i],
            isActive: i == activeStep,
            isComplete: i < activeStep,
          ),
        ],
      ],
    );
  }

  Widget _buildActions(ThemeData theme) {
    switch (_phase) {
      case _Phase.intro:
        return Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            TextButton(
              onPressed: _skip,
              child: const Text('Skip'),
            ),
            const SizedBox(width: 8),
            FilledButton.icon(
              onPressed: _startCalibration,
              icon: const Icon(Icons.play_arrow, size: 18),
              label: const Text('Start'),
            ),
          ],
        );
      case _Phase.ambient:
      case _Phase.sampling:
      case _Phase.verifying:
        return Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            TextButton(
              onPressed: _skip,
              child: const Text('Cancel'),
            ),
          ],
        );
      case _Phase.done:
        return Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            FilledButton(
              onPressed: _finish,
              child: const Text('Done'),
            ),
          ],
        );
    }
  }
}

class _StepDot extends StatelessWidget {
  const _StepDot({
    required this.label,
    required this.isActive,
    required this.isComplete,
  });

  final String label;
  final bool isActive;
  final bool isComplete;

  @override
  Widget build(BuildContext context) {
    final color = isComplete
        ? Colors.green
        : isActive
            ? Theme.of(context).colorScheme.primary
            : Colors.grey.shade400;
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 24,
          height: 24,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: isComplete || isActive ? color : Colors.transparent,
            border: Border.all(color: color, width: 2),
          ),
          child: isComplete
              ? const Icon(Icons.check, size: 14, color: Colors.white)
              : null,
        ),
        const SizedBox(height: 4),
        Text(
          label,
          style: TextStyle(fontSize: 10, color: color),
        ),
      ],
    );
  }
}
