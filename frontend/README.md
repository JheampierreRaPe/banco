# frontend — Banca Online (Flutter, F-T01)

App cliente de banca (Android/iOS) y vistas de comercio. Base creada en `F-T01`
(`docs/tasks/F-T01.md`): tema Material 3, `go_router` con guarda de sesion,
`dio` con interceptores y manejo estandar de errores. Sin logica de negocio.

- Stack: Flutter/Dart, `go_router`, `dio`, `uuid`.
  (`flutter_secure_storage` se declara en F-T02, no aqui; no hay codigo muerto.)
- El dispositivo **no** ejecuta modelos de IA; solo captura y envia al backend.
- La biometria del dispositivo se usa para login, recuperacion, pagos sensibles
  y firma (F-T03 en adelante).
- El liveness del microservicio KYC se usa **solo** en creacion de cuenta (HU01).
- Sin API keys embebidas; datos sensibles enmascarados por defecto.
- Toda accion que mueve dinero usa `Idempotency-Key`
  (`ApiClient.postWithIdempotency`).

Ver `docs/13-frontend-flutter-panel.md`.

## Estructura

```text
lib/
  main.dart                    # crea sesion en memoria y arranca la app
  app.dart                     # BancaOnlineApp (Material 3 + router)
  core/
    config/app_config.dart     # API_BASE_URL + /api/v1
    theme/app_theme.dart       # temas claro/oscuro banca
    router/app_router.dart     # ORQUESTADOR go_router (unico que agrega rutas)
    session/session_repository.dart            # seam abstracto (F-T02 lo persiste)
    session/in_memory_session_repository.dart  # tokens SOLO en memoria
    http/api_client.dart + auth/request_id interceptores
    errors/api_exception.dart + error_messages.dart  # error.code -> ES
    widgets/loading_view.dart, empty_view.dart, error_view.dart
  features/
    README.md                  # convencion: cada feature expone List<GoRoute>
    auth/auth_routes.dart      # authRoutes -> /login (placeholder)
    home/home_routes.dart      # homeRoutes -> /home (placeholder)
```

## Configurar API_BASE_URL

La base por defecto es `http://10.0.2.2:8000` (backend local visto desde el
**emulador Android**). El prefijo `/api/v1` se agrega solo (`AppConfig.apiUrl`).

```bash
# Emulador Android contra backend local (default, puedes omitirlo):
flutter run --dart-define=API_BASE_URL=http://10.0.2.2:8000

# Dispositivo fisico: usa la IP LAN de tu PC (mismo Wi-Fi) y el puerto 8000:
flutter run --dart-define=API_BASE_URL=http://192.168.1.10:8000

# iOS simulator contra backend local:
flutter run --dart-define=API_BASE_URL=http://127.0.0.1:8000
```

> El backend debe escuchar en `0.0.0.0:8000` (no solo `127.0.0.1`) para que el
> dispositivo fisico lo alcance. Verifica con `curl http://<IP>:8000/api/v1/...`
> desde el movil si falla.

## Sesion / tokens (seam F-T02)

- `SessionRepository` (abstracto, `Listenable`): `currentAccessToken`,
  `isAuthenticated`, `saveSession`, `clear`.
- `InMemorySessionRepository`: guarda tokens SOLO en memoria + notifica a
  `go_router` (`refreshListenable`). F-T02 la reemplaza con
  `flutter_secure_storage` sin cambiar la interfaz ni el router.

## Comandos

```bash
flutter pub get
flutter test
flutter analyze
flutter run [--dart-define=API_BASE_URL=...]
```
