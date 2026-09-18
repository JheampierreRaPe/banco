# Convencion de features (F-T01)

Cada feature vive en `lib/features/<nombre>/` y expone sus rutas en un archivo
`<nombre>_routes.dart`:

```dart
// lib/features/pagos/pagos_routes.dart
import 'package:go_router/go_router.dart';

final List<GoRoute> pagosRoutes = [
  GoRoute(path: '/pagos', builder: (context, state) => const PagosPage()),
];
```

Reglas:

1. El feature NUNCA toca `lib/core/router/app_router.dart`.
2. El orquestador (`app_router.dart`) importa `<feature>_routes.dart` y hace
   `...<feature>Routes` en su lista `routes`.
3. Pantallas con estados `LoadingView` / `EmptyView` / `ErrorView` de
   `lib/core/widgets/`.
4. POST que mueven dinero usan `ApiClient.postWithIdempotency`
   (`Idempotency-Key`).
5. Sin API keys embebidas; sin modelos de IA en el dispositivo; datos sensibles
   enmascarados por defecto.

## Flujo inicial (`features/welcome` + guarda de `app_router.dart`)

La primera apertura muestra `/welcome` (bienvenida de la banca + botón
"Comenzar" que persiste el flag `hasSeenWelcome` una sola vez); después, y en
aperturas siguientes sin sesión, la puerta es `/entry` ("Iniciar sesión" →
`/login`, "Crear cuenta" → `/kyc`). Públicas sin sesión: `/welcome`, `/entry`,
`/login`, `/kyc*`, `/activate`; todo lo demás exige sesión y con sesión
`/welcome`, `/entry` y `/login` redirigen a `/home`.
