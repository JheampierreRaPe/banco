import 'package:flutter/material.dart';

import 'app.dart';
import 'core/session/secure_key_value_storage.dart';
import 'core/session/secure_session_repository.dart';

/// Entrada F-T02: sesion persistente en almacenamiento seguro.
///
/// `load()` hidrata el caché del access token antes del primer frame para
/// que la guarda de `go_router` decida la ruta inicial correcta.
void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final session = SecureSessionRepository(
    storage: FlutterSecureStorageAdapter(),
  );
  await session.load();
  runApp(BancaOnlineApp(session: session));
}
