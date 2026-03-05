import 'package:flutter/material.dart';

ThemeData buildChiliTheme() {
  const primaryColor = Color(0xFFEF5350); // warm chili red
  const accentColor = Color(0xFF42A5F5); // calm blue accents

  final base = ThemeData.light();
  return base.copyWith(
    colorScheme: base.colorScheme.copyWith(
      primary: primaryColor,
      secondary: accentColor,
    ),
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

