import 'dart:async';

import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/realtime/live_channel.dart';
import 'package:chili_mobile/src/realtime/live_sources.dart';
import 'package:chili_mobile/src/realtime/live_status.dart';
import 'package:chili_mobile/src/realtime/realtime_schedule.dart';
import 'package:chili_mobile/src/realtime/realtime_service.dart';

/// A controllable source for tests — drive values/errors/done by hand.
class _FakeSource<T> implements LiveSource<T> {
  final StreamController<T> ctrl = StreamController<T>.broadcast();
  bool closed = false;
  int pokes = 0;

  @override
  Stream<T> open() => ctrl.stream;

  @override
  void poke() => pokes++;

  @override
  Future<void> close() async {
    closed = true;
    await ctrl.close();
  }
}

void main() {
  group('realtime_schedule', () {
    test('backoffDelay is exponential and capped', () {
      expect(backoffDelay(0), Duration.zero);
      expect(backoffDelay(1), const Duration(seconds: 1));
      expect(backoffDelay(2), const Duration(seconds: 2));
      expect(backoffDelay(3), const Duration(seconds: 4));
      expect(backoffDelay(4), const Duration(seconds: 8));
      // capped at 30s
      expect(backoffDelay(20), const Duration(seconds: 30));
      // huge attempt must not overflow
      expect(backoffDelay(9999), const Duration(seconds: 30));
    });

    test('adaptiveInterval is fast when active, slow when idle', () {
      expect(adaptiveInterval(active: true), const Duration(seconds: 5));
      expect(adaptiveInterval(active: false), const Duration(seconds: 30));
      expect(
        adaptiveInterval(
            active: true, activeInterval: const Duration(seconds: 2)),
        const Duration(seconds: 2),
      );
    });
  });

  group('LiveChannel', () {
    test('starts idle, goes connecting → live on first value', () async {
      final _FakeSource<int> src = _FakeSource<int>();
      final LiveChannel<int> ch = LiveChannel<int>(() => src);
      expect(ch.status, LiveStatus.idle);

      ch.start();
      expect(ch.status, LiveStatus.connecting);
      expect(ch.isActive, isTrue);

      src.ctrl.add(7);
      await Future<void>.delayed(Duration.zero);
      expect(ch.status, LiveStatus.live);
      expect(ch.value, 7);
      expect(ch.lastUpdatedMs, isNotNull);

      await ch.stop();
      expect(ch.status, LiveStatus.idle);
      expect(src.closed, isTrue);
    });

    test('errors move status to error but keep the last value', () async {
      final _FakeSource<int> src = _FakeSource<int>();
      final LiveChannel<int> ch = LiveChannel<int>(() => src);
      ch.start();
      src.ctrl.add(3);
      await Future<void>.delayed(Duration.zero);
      src.ctrl.addError(StateError('boom'));
      await Future<void>.delayed(Duration.zero);
      expect(ch.status, LiveStatus.error);
      expect(ch.error, isA<StateError>());
      expect(ch.value, 3, reason: 'last good value retained');
      await ch.stop();
    });

    test('dedupes identical values (no churn) but updates on change', () async {
      final _FakeSource<int> src = _FakeSource<int>();
      final LiveChannel<int> ch = LiveChannel<int>(() => src);
      int notifications = 0;
      ch.addListener(() => notifications++);
      ch.start(); // connecting → +1
      src.ctrl.add(1);
      await Future<void>.delayed(Duration.zero); // live → +1
      final int afterFirst = notifications;
      src.ctrl.add(1); // identical → no notify
      await Future<void>.delayed(Duration.zero);
      expect(notifications, afterFirst, reason: 'identical value deduped');
      src.ctrl.add(2); // changed → notify
      await Future<void>.delayed(Duration.zero);
      expect(notifications, greaterThan(afterFirst));
      expect(ch.value, 2);
      await ch.stop();
    });

    test('refresh pokes the source', () async {
      final _FakeSource<int> src = _FakeSource<int>();
      final LiveChannel<int> ch = LiveChannel<int>(() => src);
      ch.start();
      ch.refresh();
      ch.refresh();
      expect(src.pokes, 2);
      await ch.stop();
    });

    test('done → offline', () async {
      final _FakeSource<int> src = _FakeSource<int>();
      final LiveChannel<int> ch = LiveChannel<int>(() => src);
      ch.start();
      await src.ctrl.close();
      await Future<void>.delayed(Duration.zero);
      expect(ch.status, LiveStatus.offline);
    });
  });

  group('PollingLiveSource', () {
    test('emits the first fetch result', () async {
      final PollingLiveSource<String> src =
          PollingLiveSource<String>(() async => 'hello');
      final String first = await src.open().first;
      expect(first, 'hello');
      await src.close();
    });

    test('surfaces a fetch error on the stream', () async {
      final PollingLiveSource<String> src =
          PollingLiveSource<String>(() async => throw StateError('nope'));
      await expectLater(src.open().first, throwsA(isA<StateError>()));
      await src.close();
    });
  });

  group('RealtimeService', () {
    test('registers, returns existing, and lifecycles channels', () async {
      final RealtimeService svc = RealtimeService();
      final _FakeSource<int> src = _FakeSource<int>();
      final LiveChannel<int> a =
          svc.channel<int>('x', () => LiveChannel<int>(() => src));
      final LiveChannel<int> b =
          svc.channel<int>('x', () => LiveChannel<int>(() => _FakeSource<int>()));
      expect(identical(a, b), isTrue, reason: 'same id → same channel');
      expect(svc.ids, contains('x'));

      svc.startAll();
      expect(a.isActive, isTrue);
      await svc.stopAll();
      expect(a.isActive, isFalse);
      await svc.dispose();
      expect(svc.ids, isEmpty);
    });
  });

  group('LiveStatus', () {
    test('labels + isHealthy', () {
      expect(LiveStatus.live.label, 'Live');
      expect(LiveStatus.live.isHealthy, isTrue);
      expect(LiveStatus.offline.isHealthy, isFalse);
      expect(LiveStatus.error.label, 'Reconnecting…');
    });
  });
}
