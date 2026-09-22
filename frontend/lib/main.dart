import 'package:flutter/material.dart';

import 'app.dart';
import 'core/session/secure_key_value_storage.dart';
import 'core/session/secure_session_repository.dart';
import 'core/session/session_identity_store.dart';

/// Entrada F-T02/F-T20: sesion e identidad local en almacenamiento seguro.
///
/// [`SecureSessionRepository.load`] hidrata el cache del access token y
/// [`SecureSessionIdentityStore.load`] el `user_ref`/`device_id`; ademas se
/// asegura el `device_id` (UUID estable) ANTES del primer frame, para que la
/// guarda de `go_router` y `/login` reciban los valores correctos.
void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final storage = FlutterSecureStorageAdapter();
  final session = SecureSessionRepository(storage: storage);
  final identity = SecureSessionIdentityStore(storage: storage);
  try {
    await session.load();
    await identity.load();
    await identity.getOrCreateDeviceId();
  } catch (_) {
    // Best-effort: un fallo del secure storage (p. ej. Keystore/Keychain no
    // disponible) no debe impedir el arranque. La app abre con el cache local
    // vacio y el device_id en memoria; se rehidrata bajo demanda.
  }
  sessionIdentityStoreFactory = () => identity;
  runApp(BancaOnlineApp(session: session, identity: identity));
}
