import 'package:dio/dio.dart';

import 'session_repository.dart';

/// Cierre de sesion best-effort (F-T02).
///
/// 1. Intenta `POST /auth/logout` con `{"refresh_token": ...}` para revocar
///    en el servidor. El backend es idempotente (desconocido o doble logout
///    responden 200) y el refresh posterior queda rechazado (shape
///    verificado en `backend/tests/test_sessions.py`).
/// 2. Limpia el almacenamiento local SIEMPRE, incluso si la red falla.
/// 3. Nunca loguea tokens (docs/16 reglas 7 y 10).
class SessionService {
  SessionService({required Dio dio, required SessionRepository session}) {
    _dio = dio;
    _session = session;
  }

  late final Dio _dio;
  late final SessionRepository _session;

  Future<void> logout() async {
    final refreshToken = await _session.readRefreshToken();
    try {
      if (refreshToken != null && refreshToken.isNotEmpty) {
        await _dio.post('/auth/logout', data: {'refresh_token': refreshToken});
      }
    } on DioException {
      // Best-effort: el servidor revocara por expiracion/rotacion; lo local
      // siempre se limpia abajo.
    }
    await _session.clearOnLogout();
  }
}
