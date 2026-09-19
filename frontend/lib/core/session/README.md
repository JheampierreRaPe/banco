# Sesión y almacenamiento seguro (F-T02)

`lib/core/session/` concentra el seam de sesión:

| Archivo | Rol |
|---|---|
| `session_repository.dart` | Interfaz (access en memoria + refresh/secreto async + limpiezas). |
| `secure_session_repository.dart` | Implementación productiva sobre `flutter_secure_storage`. |
| `secure_key_value_storage.dart` | Abstracción del storage (`FlutterSecureStorageAdapter` / `InMemorySecureStorage` para tests). |
| `session_service.dart` | `logout()` best-effort (`POST /auth/logout` + limpieza local siempre). |
| `in_memory_session_repository.dart` | Solo memoria (F-T01, tests de UI/router). |
| `session_identity_store.dart` | Identidad local (F-T20): `user_ref` + `device_id` sobre secure storage. |

`lib/core/http/auth_interceptor.dart` adjunta `Bearer`, renueva UNA vez ante
401 vía `POST /auth/refresh` (`{"refresh_token"}` → `{data: {access_token,
refresh_token, ...}}`) y limpia todo + avisa si el refresh falla.

Claves en secure storage: `session.access_token`, `session.refresh_token`,
`device.secret`, `user.ref`, `device.id`.

## Identidad local (F-T20)

`SessionIdentityStore` (`SecureSessionIdentityStore` en producción,
`InMemorySessionIdentityStore` en tests) conserva el último `user_ref` (UUID
del alta, E1-T24) y un `device_id` UUID estable. Una sola cuenta activa por
dispositivo (`saveUserRef` sobreescribe). `main()` hidrata el store y asegura
el `device_id` antes del primer frame; `buildRouter` lo inyecta en
`LoginRouteDeps` (la ruta `/login` puede sobreescribirlo por query params).

## Secreto de dispositivo (seam F-T03)

`getOrCreateDeviceSecret()` guarda un secreto aleatorio seguro (32 bytes,
base64) que F-T03 usará para HMAC del nonce de login biométrico. Decisión
documentada: la criptografía asimétrica real (par de claves en Keystore /
Keychain) se introduce cuando el backend la exija; por ahora no hay libs
nativas pesadas.

Ciclo de vida: el secreto se CONSERVA en `clearOnLogout` (identifica al
dispositivo, no a la sesión) y se ROTA en `clearOnInvalidRefresh` (posible
robo, coherente con el backend que revoca toda la cadena ante reuso).
