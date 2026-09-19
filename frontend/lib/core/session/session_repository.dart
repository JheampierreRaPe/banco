import 'dart:convert';

import 'package:flutter/foundation.dart';

/// Derivacion pura de la clave publica de binding a partir del `device_secret`
/// (base64url de 32 bytes de F-T02): devuelve `"hmac:<hex>"` en hex
/// minusculas, el mismo material con el que F-T03 firma el `nonce`.
///
/// Es material PUBLICO (identifica al dispositivo): el secreto en bytes nunca
/// se devuelve ni se loguea (docs/16 reglas 7 y 10).
String deviceBindingKeyFromSecret(String deviceSecret) {
  final bytes = base64Url.decode(deviceSecret);
  final buffer = StringBuffer('hmac:');
  for (final byte in bytes) {
    buffer.write(byte.toRadixString(16).padLeft(2, '0'));
  }
  return buffer.toString();
}

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

  /// Clave PUBLICA de binding derivada del `device_secret` (F-T03/F-T22):
  /// `"hmac:<hex>"` estable e idempotente. Solo viaja esta forma publica al
  /// backend (`POST /auth/login/pin`); el secreto nunca sale del
  /// almacenamiento seguro ni se loguea (docs/16 reglas 7 y 10).
  ///
  /// Implementacion por defecto reutilizada por los repositorios en memoria
  /// de test; [SecureSessionRepository] la especializa para documentarlo.
  Future<String> getOrCreateDeviceBindingKey() async =>
      deviceBindingKeyFromSecret(await getOrCreateDeviceSecret());

  /// Limpieza local al cerrar sesion: borra tokens, CONSERVA el secreto de
  /// dispositivo (identifica al dispositivo, no a la sesion: dispositivo
  /// confiable HU04 y nonce F-T03 sin re-enrolar).
  Future<void> clearOnLogout();

  /// Limpieza total al detectar refresh invalido/revocado (posible robo,
  /// coherente con el backend que revoca toda la cadena ante reuso):
  /// borra tokens Y el secreto de dispositivo (se regenera bajo demanda).
  Future<void> clearOnInvalidRefresh();
}
