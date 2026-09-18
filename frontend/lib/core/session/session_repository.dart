import 'package:flutter/foundation.dart';

/// Seam de sesion.
///
/// F-T01 aporto saber si hay sesion y el access token para el interceptor
/// `Authorization`. F-T02 extiende el seam SIN romperlo: la persistencia vive
/// en `flutter_secure_storage` ([SecureSessionRepository]) y el interceptor
/// renueva el access con el refresh. Toda implementacion sigue siendo un
/// [Listenable] para que `go_router` (`refreshListenable`) reaccione a
/// login/logout/expiracion.
///
/// Regla: tokens y clave de dispositivo NUNCA en texto plano ni en logs
/// (docs/16 reglas 7 y 10); solo en almacenamiento seguro.
abstract class SessionRepository implements Listenable {
  /// Token de acceso actual (caché en memoria hidratado desde secure
  /// storage). `null` = sin sesion. Síncrono porque `dio.onRequest` lo es.
  String? get currentAccessToken;

  /// `true` si hay sesion activa.
  bool get isAuthenticated;

  /// Guarda una sesion (access + refresh rotativo en secure storage).
  Future<void> saveSession({
    required String accessToken,
    String? refreshToken,
  });

  /// Cierra la sesion (equivale a [clearOnLogout]).
  Future<void> clear();

  /// Refresh token actual desde secure storage. `null` = sin refresh.
  Future<String?> readRefreshToken();

  /// Secreto de dispositivo para la firma de nonce (seam F-T03): lo crea
  /// (32 bytes aleatorios seguros, base64) si no existe y lo conserva.
  Future<String> getOrCreateDeviceSecret();

  /// Limpieza local al cerrar sesion: borra tokens, CONSERVA el secreto de
  /// dispositivo (identifica al dispositivo, no a la sesion: dispositivo
  /// confiable HU04 y nonce F-T03 sin re-enrolar).
  Future<void> clearOnLogout();

  /// Limpieza total al detectar refresh invalido/revocado (posible robo,
  /// coherente con el backend que revoca toda la cadena ante reuso):
  /// borra tokens Y el secreto de dispositivo (se regenera bajo demanda).
  Future<void> clearOnInvalidRefresh();
}
