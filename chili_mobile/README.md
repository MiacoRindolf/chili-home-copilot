# CHILI Mobile (Flutter)

This is a native Flutter client for the **CHILI Home Copilot** FastAPI backend.

## Getting started

1. Install Flutter and run `flutter pub get` in the `chili_mobile` directory.
2. Make sure the CHILI backend is running locally (default `http://localhost:8000`).
3. Update `ChiliApiClient.baseUrl` in `lib/src/network/chili_api_client.dart` if needed
   (for Android emulators, `http://10.0.2.2:8000` is usually correct).
4. Run the app:

```bash
cd chili_mobile
flutter run
```

The current MVP focuses on:

- A chat-first experience with CHILI
- A simple animated avatar (Lottie-based) that reflects basic states
- A clean, minimal Material UI aligned with the CHILI web app

