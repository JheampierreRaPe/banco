# frontend (Flutter)

App cliente de banca (Android/iOS) y vistas de comercio. El proyecto Flutter se crea en la tarea
`F-T01` (`docs/tasks/F-T01.md`).

- Stack: Flutter/Dart, Riverpod o Bloc, `go_router`, `dio`, `flutter_secure_storage`, `local_auth`,
  `camera`, `qr_flutter`, `mobile_scanner`.
- El dispositivo **no** ejecuta modelos de IA; solo captura y envia al backend.
- La biometria del dispositivo se usa para login, recuperacion, pagos sensibles y firma de contrato.
- El liveness del microservicio KYC se usa **solo** en la creacion de cuenta (HU01).

Ver `docs/13-frontend-flutter-panel.md`.
