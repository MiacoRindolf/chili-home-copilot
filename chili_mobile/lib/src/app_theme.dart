import 'package:flutter/material.dart';

ThemeData buildChiliTheme() {
  return buildChiliLightTheme();
}

ThemeData buildChiliLightTheme() {
  const primaryColor = Color(0xFFEF5350); // warm chili red
  const accentColor = Color(0xFF42A5F5); // calm blue accents

  final scheme = ColorScheme.fromSeed(
    seedColor: primaryColor,
    brightness: Brightness.light,
  ).copyWith(
    primary: primaryColor,
    secondary: accentColor,
    surface: const Color(0xFFFFFFFF),
    surfaceContainerHighest: const Color(0xFFF2F4F8),
  );
  final base = ThemeData.light();
  return base.copyWith(
    colorScheme: scheme,
    scaffoldBackgroundColor: const Color(0xFFF5F5F5),
    appBarTheme: const AppBarTheme(
      backgroundColor: primaryColor,
      foregroundColor: Colors.white,
      elevation: 0,
    ),
    inputDecorationTheme: const InputDecorationTheme(
      border: OutlineInputBorder(
        borderRadius: BorderRadius.all(Radius.circular(24)),
      ),
    ),
  );
}

ThemeData buildChiliDarkTheme() {
  const primaryColor = Color(0xFFFF6B66);
  const accentColor = Color(0xFF64B5F6);
  const background = Color(0xFF111318);
  const surface = Color(0xFF181B22);

  final scheme = ColorScheme.fromSeed(
    seedColor: primaryColor,
    brightness: Brightness.dark,
  ).copyWith(
    primary: primaryColor,
    secondary: accentColor,
    surface: surface,
    surfaceContainerHighest: const Color(0xFF262A34),
    onSurface: const Color(0xFFE8EAED),
    onSurfaceVariant: const Color(0xFFC3C7D0),
  );
  final base = ThemeData.dark();
  return base.copyWith(
    colorScheme: scheme,
    scaffoldBackgroundColor: background,
    cardColor: surface,
    dividerColor: const Color(0xFF333844),
    appBarTheme: const AppBarTheme(
      backgroundColor: Color(0xFF181B22),
      foregroundColor: Color(0xFFE8EAED),
      elevation: 0,
    ),
    inputDecorationTheme: const InputDecorationTheme(
      border: OutlineInputBorder(
        borderRadius: BorderRadius.all(Radius.circular(24)),
      ),
    ),
  );
}

ThemeMode chiliThemeModeFromString(String value) {
  switch (value) {
    case 'light':
      return ThemeMode.light;
    case 'dark':
      return ThemeMode.dark;
    case 'system':
    default:
      return ThemeMode.system;
  }
}
