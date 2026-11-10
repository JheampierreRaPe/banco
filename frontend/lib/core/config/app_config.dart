// Base URL y prefijo de la API.
//
// Se configura con `--dart-define=API_BASE_URL=...`.
// Por defecto apunta al backend local visto desde el emulador Android.
class AppConfig {
  AppConfig._();

  /// Host del backend (sin prefijo de version).
  static const String apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://10.0.2.2:8000',
  );

  /// Prefijo de version segun docs/05 (`/api/v1`).
  static const String apiPrefix = '/api/v1';

  /// URL completa contra la que habla `dio` (p. ej. `http://10.0.2.2:8000/api/v1`).
  static String get apiUrl => '$apiBaseUrl$apiPrefix';
}
