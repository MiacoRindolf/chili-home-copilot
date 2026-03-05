import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

/// One-time downloader for the Vosk native library (libvosk.dll) and a small
/// English speech model.  Everything is cached under the app's support
/// directory so subsequent starts are instant.
class VoskSetup {
  static const _voskVersion = '0.3.45';
  static const _modelName = 'vosk-model-small-en-us-0.15';

  static const _dllUrl =
      'https://github.com/alphacep/vosk-api/releases/download/'
      'v$_voskVersion/vosk-win64-$_voskVersion.zip';
  static const _modelUrl =
      'https://alphacephei.com/vosk/models/$_modelName.zip';

  /// Returns the absolute path to `libvosk.dll`, downloading & extracting on
  /// the first call.
  static Future<String> ensureDll({
    void Function(String)? onProgress,
  }) async {
    final dir = await _dataDir();
    final dllPath = p.join(dir.path, 'libvosk.dll');
    final extractedDir = Directory(
      p.join(dir.path, 'vosk-win64-$_voskVersion'),
    );

    // Ensure we have the extracted runtime folder (download+extract once).
    if (!extractedDir.existsSync()) {
      onProgress?.call('Downloading Vosk speech engine...');
      debugPrint('[VoskSetup] Downloading DLL from $_dllUrl');
      final zipPath = p.join(dir.path, 'vosk-win64.zip');

      try {
        await _download(_dllUrl, zipPath);
      } catch (e) {
        _tryDelete(zipPath);
        rethrow;
      }

      onProgress?.call('Extracting speech engine...');
      await _extractZip(zipPath, dir.path);
      _tryDelete(zipPath);
    }

    // Copy all DLLs from the extracted folder next to each other in the root
    // `vosk` directory so Windows can resolve libvosk's dependencies.
    if (extractedDir.existsSync()) {
      for (final entry in extractedDir.listSync()) {
        if (entry is File && p.extension(entry.path).toLowerCase() == '.dll') {
          final target = p.join(dir.path, p.basename(entry.path));
          if (!File(target).existsSync()) {
            entry.copySync(target);
          }
        }
      }
    }

    if (!File(dllPath).existsSync()) {
      throw StateError('libvosk.dll not found after extraction');
    }
    debugPrint('[VoskSetup] DLL ready at $dllPath');
    return dllPath;
  }

  /// Returns the absolute path to the extracted model directory, downloading
  /// on the first call (~40 MB one-time download).
  static Future<String> ensureModel({
    void Function(String)? onProgress,
  }) async {
    final dir = await _dataDir();
    final modelDir = p.join(dir.path, _modelName);

    if (Directory(modelDir).existsSync()) {
      final marker = File(p.join(modelDir, 'conf', 'mfcc.conf'));
      if (marker.existsSync()) return modelDir;
    }

    onProgress?.call('Downloading speech model (~40 MB, one-time)...');
    debugPrint('[VoskSetup] Downloading model from $_modelUrl');
    final zipPath = p.join(dir.path, '$_modelName.zip');

    try {
      await _download(_modelUrl, zipPath);
    } catch (e) {
      _tryDelete(zipPath);
      rethrow;
    }

    onProgress?.call('Extracting speech model...');
    await _extractZip(zipPath, dir.path);
    _tryDelete(zipPath);

    if (!Directory(modelDir).existsSync()) {
      throw StateError('Model directory not found after extraction');
    }
    debugPrint('[VoskSetup] Model ready at $modelDir');
    return modelDir;
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  static Future<Directory> _dataDir() async {
    final appDir = await getApplicationSupportDirectory();
    final d = Directory(p.join(appDir.path, 'vosk'));
    if (!d.existsSync()) {
      d.createSync(recursive: true);
    }
    return d;
  }

  static Future<void> _download(String url, String dest) async {
    final client = HttpClient();
    try {
      final req = await client.getUrl(Uri.parse(url));
      final resp = await req.close();
      if (resp.statusCode != 200) {
        throw HttpException(
          'HTTP ${resp.statusCode} downloading $url',
          uri: Uri.parse(url),
        );
      }
      await resp.pipe(File(dest).openWrite());
    } finally {
      client.close();
    }
  }

  static Future<void> _extractZip(String zipPath, String destDir) async {
    final result = await Process.run('powershell', [
      '-NoProfile',
      '-Command',
      'Expand-Archive -Path "$zipPath" -DestinationPath "$destDir" -Force',
    ]);
    if (result.exitCode != 0) {
      debugPrint('[VoskSetup] Expand-Archive stderr: ${result.stderr}');
      throw StateError('Failed to extract $zipPath');
    }
  }

  static void _tryDelete(String path) {
    try {
      File(path).deleteSync();
    } catch (_) {}
  }
}
