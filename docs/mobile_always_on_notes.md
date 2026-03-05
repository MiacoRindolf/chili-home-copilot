# CHILI Mobile “Always-On” Presence Notes

## Android

- Use a foreground service with a persistent notification as a quick entry point.
- Consider chat-head style bubbles using the system overlay APIs (requires special permission and battery impact trade-offs).
- Deep-link notifications into the current conversation in the Flutter app.

## iOS

- True floating avatars are not supported for third-party apps.
- Use widgets, Live Activities, or (on supported devices) Dynamic Island to surface CHILI contextually.
- Rely on push notifications with rich content to bring users back into the app quickly.

## Shared considerations

- Respect battery and privacy; avoid aggressive background polling.
- Make the always-on behavior explicitly opt-in with clear settings in the app.
- Start with simple notification-based presence before more advanced overlays.

