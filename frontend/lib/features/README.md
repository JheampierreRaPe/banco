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

## Flujo inicial (`features/welcome` + `features/splash` + guarda de `app_router.dart`)

El arranque entra por `/splash` (F-T37): la splash delega en la guarda del
router, que resuelve por sesión + `userRef` de `SessionIdentityStore`
(F-T36/F-T37, decisión SCR-005 d5; sin flag local de bienvenida).
Sin sesión y sin `userRef`, la puerta es `/welcome` (`OnboardingPage`:
PageView de 3 slides, "Crear mi cuenta" → `/kyc`, "Ya tengo cuenta ·
Iniciar sesión" → `/recovery`). El legacy `/entry` quedó retirado (F-T36)
y redirige a `/welcome` sin sesión. Públicas sin sesión con `userRef`:
`/splash`, `/welcome`, `/login`, `/kyc*`, `/activate`, `/pin-setup`,
`/recovery`, `/recovery/otp`; todo lo demás exige sesión y con sesión
`/splash`, `/welcome` y `/login` redirigen a `/home`.
