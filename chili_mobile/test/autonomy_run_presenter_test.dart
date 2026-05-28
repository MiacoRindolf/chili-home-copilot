import 'package:flutter_test/flutter_test.dart';
import 'package:chili_mobile/src/brain/autonomy_run_presenter.dart';

void main() {
  test('interrupted worker run gets a human blocked message', () {
    final run = {
      'status': 'blocked',
      'error_message':
          'Autopilot worker was interrupted by an API restart before this durable run completed. Start a new run so the current process can own the worktree safely.',
    };

    expect(
      AutonomyRunPresenter.blockedRunMessage(run),
      'This run stopped because the backend restarted during implementation. No changes were merged. Start a fresh run from the same prompt.',
    );
    expect(AutonomyRunPresenter.isInterruptedWorkerRun(run), isTrue);
    expect(AutonomyRunPresenter.canRerun(run), isFalse);
  });

  test('common steps render as prose instead of json', () {
    final queued = AutonomyRunPresenter.stepBody({
      'stage': 'queued',
      'status': 'completed',
      'detail': {'repo_id': 8, 'repo_name': 'chili-home-copilot'},
    });
    final classify = AutonomyRunPresenter.stepBody({
      'stage': 'classify',
      'status': 'running',
      'detail': {'prompt_preview': 'make the desktop chat friendlier'},
    });
    final repoScan = AutonomyRunPresenter.stepBody({
      'stage': 'repo_scan',
      'status': 'running',
      'detail': {'repo': 'chili-home-copilot', 'path': '/workspace'},
    });
    final roles = AutonomyRunPresenter.stepBody({
      'stage': 'assign_roles',
      'status': 'completed',
      'detail': {
        'agents': [
          {'name': 'architect'},
          {'name': 'frontend'},
        ],
      },
    });

    expect(queued, 'Queued for chili-home-copilot (#8).');
    expect(classify, contains('Read the request'));
    expect(repoScan, 'Scanned chili-home-copilot at /workspace.');
    expect(roles, contains('architect'));
    for (final body in [queued, classify, repoScan, roles]) {
      expect(body, isNot(contains('{')));
      expect(body, isNot(contains('"repo_id"')));
    }
  });

  test('artifacts and validation summarize without json dumps', () {
    final model = AutonomyRunPresenter.artifactBody({
      'artifact_type': 'model_call',
      'name': 'plan',
      'content_json': {
        'ok': true,
        'model': 'qwen2.5-coder:7b',
        'purpose': 'planning',
        'latency_ms': 86700,
      },
    });
    final rejected = AutonomyRunPresenter.artifactBody({
      'artifact_type': 'diff_rejected',
      'content_json': {
        'reason': 'patch failed to apply',
        'stderr': 'line mismatch',
      },
    });
    final validation = AutonomyRunPresenter.validationBody([
      {'step_key': 'flutter test', 'exit_code': 0},
      {'step_key': 'flutter analyze', 'exit_code': 1},
    ]);

    expect(model, contains('qwen2.5-coder:7b'));
    expect(model, contains('86.7s'));
    expect(rejected, contains('line mismatch'));
    expect(validation, contains('1 of 2 validation checks failed'));
    for (final body in [model, rejected, validation]) {
      expect(body, isNot(contains('{')));
      expect(body, isNot(contains('"ok"')));
    }
  });

  test('validation output names skipped local gates', () {
    final validation = AutonomyRunPresenter.validationBody([
      {'step_key': 'ast_syntax', 'exit_code': 0},
      {
        'step_key': 'pytest_targeted',
        'exit_code': 0,
        'skipped': true,
        'skip_reason': 'safe TEST_DATABASE_URL not configured',
      },
      {
        'step_key': 'mypy_check',
        'exit_code': 0,
        'skipped': true,
        'skip_reason': 'mypy not available',
      },
    ]);

    expect(validation, contains('1 validation check passed; 2 skipped.'));
    expect(
      validation,
      contains('pytest_targeted skipped: safe TEST_DATABASE_URL not configured'),
    );
    expect(validation, isNot(contains('"skipped"')));
  });

  test('dirty checkout merge blocks are explained as operator next steps', () {
    final run = {
      'status': 'blocked',
      'merge_message': 'Target checkout has dirty changes touching the autopilot scope.',
    };
    final step = AutonomyRunPresenter.stepBody({
      'stage': 'merge',
      'status': 'blocked',
      'detail': {
        'merge_message': 'Target checkout has dirty changes touching the autopilot scope.',
      },
    });

    expect(AutonomyRunPresenter.blockedRunMessage(run), contains('validated branch'));
    expect(AutonomyRunPresenter.blockedRunMessage(run), contains('Commit or stash'));
    expect(step, contains('validated branch'));
    expect(step, isNot(contains('dirty changes touching the autopilot scope')));
  });

  test('empty diff blocks are explained without raw worker wording', () {
    final run = {
      'status': 'blocked',
      'error_message': 'No implementation diffs were generated.',
    };

    final message = AutonomyRunPresenter.blockedRunMessage(run);

    expect(message, contains('could not turn this run into a usable code patch'));
    expect(message, contains('No changes were merged'));
    expect(message, isNot(contains('No implementation diffs were generated')));
  });

  test('repaired historical blocked runs point to rerun instead of scary logs',
      () {
    final run = {
      'status': 'blocked',
      'prompt': 'make a small enhancement',
      'error_message':
          'Generated patch was rejected before apply: error: corrupt patch at line 15. Backend path/worktree handling and rejected-diff reporting have been repaired; rerun the prompt to use the fixed worker.',
    };

    expect(AutonomyRunPresenter.canRerun(run), isTrue);
    expect(
      AutonomyRunPresenter.blockedRunMessage(run),
      'This run ended before the backend repair could help it. Start a fresh run from the same prompt to use the fixed worker.',
    );
  });

  test('unknown maps fall back to compact key value prose', () {
    final body = AutonomyRunPresenter.compact({
      'repo_id': 8,
      'repo_name': 'chili-home-copilot',
      'nested': {'path': 'lib/main.dart'},
    });

    expect(body, contains('Repo id: 8'));
    expect(body, contains('Repo name: chili-home-copilot'));
    expect(body, contains('Nested: lib/main.dart'));
    expect(body, isNot(contains('{')));
  });
}
