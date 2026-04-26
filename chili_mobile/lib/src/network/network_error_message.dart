/// Human-readable network errors for UI (avoids dumping raw [SocketException] stacks).
String userVisibleNetworkError(Object error) {
  final s = error.toString();
  if (s.contains('10055') ||
      s.contains('buffer space') ||
      s.contains('WSAENOBUFS') ||
      s.contains('No buffer space available')) {
    return 'This PC could not open a network connection (Windows error 10055 - '
        'socket or buffer space exhausted). Try: close other apps, exit Docker or VPN, '
        'restart this app, or restart Windows. The Backend URL is often still correct; '
        'this is a local Windows limit, not a bad server address.';
  }
  if (s.contains('Failed host lookup') || s.contains('getaddrinfo')) {
    return 'Could not resolve the server hostname. Check internet and the Backend URL in Settings.';
  }
  if (s.contains('Connection refused')) {
    return 'Connection refused. Is the server running, and is the Backend URL in Settings correct?';
  }
  if (s.contains('timed out') || s.contains('TimeoutException')) {
    return 'Connection timed out. Check the Backend URL and firewall / VPN.';
  }
  return s;
}

/// Short copy when the HTTP response is an error and the body is not app JSON (e.g. 502 HTML from a proxy).
String userMessageForHttpStatus(int statusCode) {
  switch (statusCode) {
    case 502:
      return 'The server is temporarily unavailable (502 bad gateway). A proxy in front of '
          'the site (for example Cloudflare) could not reach the CHILI backend. Try again later, '
          'or in Settings set Backend URL to a server you know is up (e.g. local https://localhost:8000).';
    case 503:
      return 'Service unavailable (503). The server is busy or in maintenance. Try again later.';
    case 504:
      return 'Gateway timeout (504). The upstream server was too slow. Try again later.';
    default:
      if (statusCode >= 500) {
        return 'Server error (HTTP ' + statusCode.toString() + '). The backend may be down. Try again later or change the Backend URL in Settings.';
      }
      if (statusCode >= 400) {
        return 'Request was rejected (HTTP ' + statusCode.toString() + '). Check the Backend URL and try again.';
      }
      return 'Unexpected HTTP ' + statusCode.toString() + ' from server.';
  }
}

