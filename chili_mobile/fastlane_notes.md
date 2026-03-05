# Release Pipeline Notes for CHILI Mobile

## Android (Google Play)

- Configure an application in Google Play Console for "CHILI Mobile".
- Use Flutter's standard build commands:
  - `flutter build appbundle --release` for AAB.
- Automate uploads with Fastlane or GitHub Actions as a future enhancement.

## iOS (App Store / TestFlight)

- Create an app record in App Store Connect.
- Use:
  - `flutter build ios --release`
  - Upload via Xcode or Transporter.
- Start with TestFlight internal testing before public release.

## Shared

- Keep `ChiliApiClient.baseUrl` configurable per environment (dev/stage/prod).
- Ensure privacy policy and terms are linked from both the app stores and within the app.

