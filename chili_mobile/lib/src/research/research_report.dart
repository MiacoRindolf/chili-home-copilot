import 'dart:io';

import 'package:url_launcher/url_launcher.dart';

import '../network/chili_api_client.dart';
import 'research_models.dart';

/// Open a research source URL in the OS browser (RS-5). Guards with isHttpUrl
/// so only http/https links launch; returns false for anything else or on
/// launch failure.
Future<bool> openExternalUrl(String url) async {
  if (!isHttpUrl(url)) return false;
  return launchUrl(Uri.parse(url.trim()),
      mode: LaunchMode.externalApplication);
}

/// Fetches the self-contained HTML research report, writes it to a temp file,
/// and opens it in the OS browser — the report renders fully offline (no
/// backend calls), so a plain `file://` open works. Returns the file path on
/// success, or null on failure. (RS-1)
Future<String?> openResearchReport(ChiliApiClient api) async {
  final String html = await api.getResearchReportHtml();
  if (html.trim().isEmpty) return null;
  final File file = File(
    '${Directory.systemTemp.path}${Platform.pathSeparator}chili-research-digest.html',
  );
  await file.writeAsString(html, flush: true);
  final bool ok = await launchUrl(
    Uri.file(file.path),
    mode: LaunchMode.externalApplication,
  );
  return ok ? file.path : null;
}
