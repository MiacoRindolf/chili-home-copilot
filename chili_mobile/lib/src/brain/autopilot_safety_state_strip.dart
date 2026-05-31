import 'package:flutter/material.dart';

import 'autopilot_safety_state_presenter.dart';

class AutopilotSafetyStateStrip extends StatelessWidget {
  const AutopilotSafetyStateStrip({
    super.key,
    required this.state,
    this.busy = false,
    this.onOpenTarget,
    this.onCopyHandoff,
    this.onOpenControlTarget,
    this.onCopyControlTarget,
  });

  final AutopilotSafetyState state;
  final bool busy;
  final VoidCallback? onOpenTarget;
  final VoidCallback? onCopyHandoff;
  final VoidCallback? onOpenControlTarget;
  final VoidCallback? onCopyControlTarget;

  @override
  Widget build(BuildContext context) {
    final color = _safetyColor(state.severity);
    final nextActionLabel = state.nextActionAgent.isEmpty
        ? state.nextActionLabel
        : '${state.nextActionAgent}: ${state.nextActionLabel}';
    final nextActionDetail = state.nextActionDetail.isEmpty
        ? 'No waiting operator action.'
        : state.nextActionDetail;
    final canOpen = state.hasOpenTarget && onOpenTarget != null;
    final canCopy = state.hasHandoff && onCopyHandoff != null;
    final canOpenControl =
        state.hasControlTargetOpen && onOpenControlTarget != null;
    final canCopyControl =
        state.hasControlTargetHandoff && onCopyControlTarget != null;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(9),
      decoration: BoxDecoration(
        color: _bubbleBackground(color, alpha: 0.07),
        border: Border.all(color: color.withValues(alpha: 0.24)),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.health_and_safety_outlined, color: color, size: 16),
              const SizedBox(width: 7),
              Expanded(
                child: Text(
                  'Safety state',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
              ),
              _miniChip(_safetyLabel(state.severity), color),
              if (state.inboxActionCount > 0) ...[
                const SizedBox(width: 5),
                _miniChip(
                  '${state.inboxActionCount} action${state.inboxActionCount == 1 ? '' : 's'}',
                  Colors.orange.shade900,
                ),
              ],
            ],
          ),
          const SizedBox(height: 7),
          _SafetyStateLine(
            icon: Icons.verified_user_outlined,
            label: state.releaseLabel,
            detail: state.releaseDetail,
            color: state.releaseBlocked ? Colors.deepOrange.shade800 : color,
          ),
          _SafetyStateLine(
            icon: Icons.speed_outlined,
            label: state.runtimeLabel,
            detail: state.runtimeDetail,
            color: state.runtimeBlocked ? Colors.deepOrange.shade800 : color,
          ),
          _SafetyStateLine(
            icon: Icons.account_tree_outlined,
            label: state.controlLabel,
            detail: state.controlDetail,
            color: state.controlBlocked ? Colors.deepOrange.shade800 : color,
          ),
          if (state.controlTargetLabel.isNotEmpty ||
              state.controlTargetDetail.isNotEmpty) ...[
            _SafetyStateLine(
              icon: Icons.gpp_maybe_outlined,
              label: state.controlTargetLabel.isEmpty
                  ? 'Control target'
                  : state.controlTargetLabel,
              detail: state.controlTargetDetail,
              color: Colors.deepOrange.shade800,
            ),
            if (canOpenControl || canCopyControl)
              Padding(
                padding: const EdgeInsets.only(top: 4, left: 20),
                child: Wrap(
                  spacing: 6,
                  runSpacing: 6,
                  children: [
                    if (canOpenControl)
                      OutlinedButton.icon(
                        style: OutlinedButton.styleFrom(
                          visualDensity: VisualDensity.compact,
                          padding: const EdgeInsets.symmetric(horizontal: 8),
                        ),
                        onPressed: busy ? null : onOpenControlTarget,
                        icon: const Icon(Icons.open_in_new, size: 15),
                        label: const Text('Open target'),
                      ),
                    if (canCopyControl)
                      IconButton(
                        tooltip: state.controlTargetHandoffLabel.isEmpty
                            ? 'Copy control handoff'
                            : state.controlTargetHandoffLabel,
                        visualDensity: VisualDensity.compact,
                        onPressed: busy ? null : onCopyControlTarget,
                        icon: const Icon(Icons.copy_all_outlined, size: 15),
                      ),
                  ],
                ),
              ),
          ],
          _SafetyStateLine(
            icon: Icons.article_outlined,
            label: state.evidenceLabel,
            detail: state.evidenceDetail,
            color: color,
          ),
          _SafetyStateLine(
            icon: state.hasAction
                ? Icons.radio_button_checked_outlined
                : Icons.check_circle_outline,
            label: nextActionLabel,
            detail: nextActionDetail,
            color: state.hasAction ? Colors.orange.shade900 : color,
          ),
          if (canOpen || canCopy) ...[
            const SizedBox(height: 7),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                if (canOpen)
                  OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(
                      visualDensity: VisualDensity.compact,
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                    ),
                    onPressed: busy ? null : onOpenTarget,
                    icon: const Icon(Icons.open_in_new, size: 15),
                    label: Text(state.safeActionButtonLabel),
                  ),
                if (canCopy)
                  IconButton(
                    tooltip: state.nextActionHandoffLabel.isEmpty
                        ? 'Copy handoff'
                        : state.nextActionHandoffLabel,
                    visualDensity: VisualDensity.compact,
                    onPressed: busy ? null : onCopyHandoff,
                    icon: const Icon(Icons.copy_all_outlined, size: 15),
                  ),
              ],
            ),
          ],
          const SizedBox(height: 6),
          Text(
            state.nonAuthorizationDetail,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              color: color,
              fontSize: 11,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }

  Widget _miniChip(String label, Color color) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
      decoration: BoxDecoration(
        color: _bubbleBackground(color),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(
          color: color,
          fontSize: 11,
          fontWeight: FontWeight.w800,
        ),
      ),
    );
  }
}

class _SafetyStateLine extends StatelessWidget {
  const _SafetyStateLine({
    required this.icon,
    required this.label,
    required this.detail,
    required this.color,
  });

  final IconData icon;
  final String label;
  final String detail;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 5),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, color: color, size: 14),
          const SizedBox(width: 6),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                if (detail.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 1),
                    child: Text(
                      detail,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.onSurfaceVariant,
                        fontSize: 11,
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

Color _safetyColor(AutopilotSafetySeverity severity) {
  switch (severity) {
    case AutopilotSafetySeverity.blocked:
      return Colors.deepOrange.shade800;
    case AutopilotSafetySeverity.warning:
      return Colors.orange.shade900;
    case AutopilotSafetySeverity.clear:
      return Colors.teal.shade900;
  }
}

String _safetyLabel(AutopilotSafetySeverity severity) {
  switch (severity) {
    case AutopilotSafetySeverity.blocked:
      return 'blocked';
    case AutopilotSafetySeverity.warning:
      return 'watch';
    case AutopilotSafetySeverity.clear:
      return 'clear';
  }
}

Color _bubbleBackground(Color color, {double alpha = 0.10}) {
  return color.withValues(alpha: alpha);
}
