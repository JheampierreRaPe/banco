import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/router/app_router.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/login/login_routes.dart';
import 'package:banca_online/features/profile/data/biometric_consent_service.dart';
import 'package:banca_online/features/profile/presentation/profile_page.dart';
import 'package:banca_online/features/profile/profile_routes.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fake del consentimiento (seam `BiometricConsentServiceBase`): registra la
/// intencion y devuelve el valor "del servidor" (`serverValue`) o lanza
/// [error]. Sin [serverValue] ni [error], refleja la intencion.
class _FakeConsent implements BiometricConsentServiceBase {
  _FakeConsent({this.serverValue, this.error});

  final bool? serverValue;
  final Object? error;
  bool? lastEnabled;
  int calls = 0;

  @override
  Future<bool> setConsent({required bool enabled}) async {
    calls++;
    lastEnabled = enabled;
    if (error != null) throw error!;
    return serverValue ?? enabled;
  }
}

Widget _harness(Widget child) => MaterialApp(home: Scaffold(body: child));

/// La pantalla (fig 390×844 + bottom-nav) excede el viewport de tests
/// (800×600): se usa un viewport alto para que todo el `ListView` quede
/// construido y visible sin scrolls intermedios.
void _tallViewport(WidgetTester tester) {
  tester.view.physicalSize = const Size(390, 2000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
}

ProfilePage _page({
  _FakeConsent? consent,
  SessionIdentityStore? identity,
  BiometricReader? reader,
  Future<void> Function()? onLogout,
}) {
  return ProfilePage(
    consentService: consent ?? _FakeConsent(),
    identity: identity ?? InMemorySessionIdentityStore(),
    reader: reader ?? FakeBiometricReader(),
    onLogout: onLogout ?? () async {},
  );
}

void main() {
  group('F-T52 pantalla opciones-usuario (fig 0:2904)', () {
    testWidgets('renderiza la estructura completa del fig', (tester) async {
      _tallViewport(tester);
      await tester.pumpWidget(_harness(_page()));
      await tester.pumpAndSettle();

      expect(find.text('Mi perfil'), findsOneWidget);
      expect(find.text('Carlos Mendoza'), findsOneWidget);
      expect(find.text('ID Cliente 4829-0193'), findsOneWidget);
      expect(find.text('TUS DATOS'), findsOneWidget);
      expect(find.text('Editar información de contacto'), findsOneWidget);
      expect(find.text('SEGURIDAD'), findsOneWidget);
      expect(find.text('Protección activa'), findsOneWidget);
      expect(find.text('Bloquear tarjeta temporalmente'), findsOneWidget);
      expect(find.text('Ingreso por biometría'), findsOneWidget);
      expect(
        find.text('Accede rápido con Face ID o huella digital'),
        findsOneWidget,
      );
      expect(find.text('Cambiar PIN y contraseñas'), findsOneWidget);
      expect(find.text('Cuentas asociadas'), findsOneWidget);
      expect(find.text('SABER MÁS'), findsOneWidget);
      expect(find.text('Bonificaciones y beneficios'), findsOneWidget);
      expect(find.text('Nuevo'), findsOneWidget);
      expect(find.text('Créditos preaprobados'), findsOneWidget);
      expect(find.text('Educación financiera'), findsOneWidget);
      expect(find.text('Cerrar sesión'), findsOneWidget);
      expect(find.text('Perfil'), findsOneWidget);
      expect(find.byKey(const Key('biometric-switch')), findsOneWidget);
      // Cliente delgado: jamas se portan agregados del `dash` (0:3057).
      expect(find.text('SALDO TOTAL'), findsNothing);
    });

    testWidgets('switch inicia OFF sin flag conocido', (tester) async {
      await tester.pumpWidget(_harness(_page()));
      await tester.pumpAndSettle();
      expect(
        tester.widget<Switch>(find.byKey(const Key('biometric-switch'))).value,
        isFalse,
      );
    });

    testWidgets('switch inicia ON con flag F-T49 sincronizado',
        (tester) async {
      await tester.pumpWidget(
        _harness(
          _page(
            identity: InMemorySessionIdentityStore(biometricEnabled: true),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(
        tester.widget<Switch>(find.byKey(const Key('biometric-switch'))).value,
        isTrue,
      );
    });

    testWidgets('enable llama al endpoint y refleja true en store y UI',
        (tester) async {
      final consent = _FakeConsent();
      final identity = InMemorySessionIdentityStore();
      await tester.pumpWidget(
        _harness(_page(consent: consent, identity: identity)),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('biometric-switch')));
      await tester.pumpAndSettle();

      expect(consent.calls, 1);
      expect(consent.lastEnabled, isTrue);
      expect(identity.biometricEnabled, isTrue);
      expect(
        tester.widget<Switch>(find.byKey(const Key('biometric-switch'))).value,
        isTrue,
      );
    });

    testWidgets('revoke llama con enabled:false y refleja false',
        (tester) async {
      final consent = _FakeConsent();
      final identity = InMemorySessionIdentityStore(biometricEnabled: true);
      await tester.pumpWidget(
        _harness(_page(consent: consent, identity: identity)),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('biometric-switch')));
      await tester.pumpAndSettle();

      expect(consent.calls, 1);
      expect(consent.lastEnabled, isFalse);
      expect(identity.biometricEnabled, isFalse);
      expect(
        tester.widget<Switch>(find.byKey(const Key('biometric-switch'))).value,
        isFalse,
      );
    });

    testWidgets('el servidor es la autoridad (no hay optimismo local)',
        (tester) async {
      // Intencion `true`, servidor responde `false`: la UI y el store
      // quedan en `false`.
      final consent = _FakeConsent(serverValue: false);
      final identity = InMemorySessionIdentityStore();
      await tester.pumpWidget(
        _harness(_page(consent: consent, identity: identity)),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('biometric-switch')));
      await tester.pumpAndSettle();

      expect(consent.lastEnabled, isTrue);
      expect(identity.biometricEnabled, isFalse);
      expect(
        tester.widget<Switch>(find.byKey(const Key('biometric-switch'))).value,
        isFalse,
      );
    });

    testWidgets('error del endpoint revierte el switch y ofrece reintento',
        (tester) async {
      final consent = _FakeConsent(error: Exception('red caída'));
      final identity = InMemorySessionIdentityStore();
      await tester.pumpWidget(
        _harness(_page(consent: consent, identity: identity)),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('biometric-switch')));
      await tester.pumpAndSettle();

      // Switch consistente (sigue OFF) + mensaje neutro (sin tecnicismos).
      // El store jamas se sincronizo: sigue `null` (sin consentimiento).
      expect(
        tester.widget<Switch>(find.byKey(const Key('biometric-switch'))).value,
        isFalse,
      );
      expect(identity.biometricEnabled, isNull);
      expect(
        find.text('No se pudo actualizar la biometria. Intentalo de nuevo.'),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('profile-biometric-retry')),
        findsOneWidget,
      );
      expect(find.text('red caída'), findsNothing);
    });

    testWidgets('sin dispositivo el switch se deshabilita con explicacion',
        (tester) async {
      final consent = _FakeConsent();
      await tester.pumpWidget(
        _harness(
          _page(
            consent: consent,
            reader: FakeBiometricReader(available: false),
          ),
        ),
      );
      await tester.pumpAndSettle();

      final sw =
          tester.widget<Switch>(find.byKey(const Key('biometric-switch')));
      expect(sw.onChanged, isNull);
      expect(
        find.text('Tu dispositivo no tiene biometría disponible.'),
        findsOneWidget,
      );
      expect(consent.calls, 0);
    });

    testWidgets('Cerrar sesion invoca onLogout (SessionService en prod)',
        (tester) async {
      _tallViewport(tester);
      var calls = 0;
      await tester.pumpWidget(
        _harness(_page(onLogout: () async => calls++)),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('profile-logout')));
      await tester.pumpAndSettle();

      expect(calls, 1);
    });

    testWidgets('B3: onLogout que falla no propaga y avisa neutro',
        (tester) async {
      _tallViewport(tester);
      var calls = 0;
      await tester.pumpWidget(
        _harness(_page(onLogout: () async {
          calls++;
          throw Exception('boom-tecnico');
        })),
      );
      await tester.pumpAndSettle();

      // Sin el `catch` la excepcion propagaba y el test fallaba aqui.
      await tester.tap(find.byKey(const Key('profile-logout')));
      await tester.pumpAndSettle();

      expect(calls, 1);
      expect(
        find.text('No se pudo cerrar la sesion. Intentalo de nuevo.'),
        findsOneWidget,
      );
      expect(find.text('boom-tecnico'), findsNothing);
      // El boton se recupera (ya no "Cerrando sesión…").
      expect(find.text('Cerrar sesión'), findsOneWidget);
    });

    testWidgets('filas visuales no disparan ninguna accion (CA-06)',
        (tester) async {
      _tallViewport(tester);
      await tester.pumpWidget(_harness(_page()));
      await tester.pumpAndSettle();

      for (final label in [
        'Editar información de contacto',
        'Bloquear tarjeta temporalmente',
        'Cambiar PIN y contraseñas',
        'Cuentas asociadas',
        'Bonificaciones y beneficios',
        'Créditos preaprobados',
        'Educación financiera',
        'Tarjetas',
        'Operar',
      ]) {
        await tester.tap(find.text(label));
        await tester.pump();
      }
      await tester.tap(find.byKey(const Key('profile-help')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('profile-edit')));
      await tester.pump();

      // Nada navego ni cambio estado: seguimos en "Mi perfil" con el
      // switch intacto y sin errores.
      expect(find.text('Mi perfil'), findsOneWidget);
      expect(find.byKey(const Key('biometric-switch')), findsOneWidget);
      expect(find.text('Reintentar'), findsNothing);
    });
  });

  group('F-T52 acceso y ruta privada /profile (CA-01)', () {
    Widget routerHarness(
      InMemorySessionRepository session,
      InMemorySessionIdentityStore identity, {
      String initial = '/profile',
      Future<void> Function()? onLogout,
    }) {
      final client = ApiClient.create(
        session: session,
        dioOverride: Dio(),
      );
      profileRouteDepsFactory = () => ProfileRouteDeps(
            api: client,
            session: session,
            identity: identity,
            reader: FakeBiometricReader(),
            onLogout: onLogout ?? () async {},
          );
      addTearDown(() => profileRouteDepsFactory = null);
      final router = buildRouter(
        session,
        identity: identity,
        initialLocation: initial,
      );
      addTearDown(router.dispose);
      return MaterialApp.router(routerConfig: router);
    }

    testWidgets('sin sesion y sin userRef /profile va al onboarding',
        (tester) async {
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      await tester.pumpWidget(routerHarness(session, identity));
      await tester.pumpAndSettle();

      expect(find.text('Mi perfil'), findsNothing);
      expect(find.text('Crear mi cuenta'), findsOneWidget);
    });

    testWidgets('sin sesion con userRef /profile va al login',
        (tester) async {
      // La pagina real de login mantiene un Timer periodico; se desactiva
      // para asentar (patron de `router_test.dart`).
      debugDisableLoginAutoTick = true;
      addTearDown(() => debugDisableLoginAutoTick = false);
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore(userRef: 'u-1');
      addTearDown(identity.dispose);
      await tester.pumpWidget(routerHarness(session, identity));
      await tester.pump(const Duration(milliseconds: 100));
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Mi perfil'), findsNothing);
      expect(find.text('Inicia sesión'), findsOneWidget);
    });

    testWidgets('con sesion /profile muestra la pantalla', (tester) async {
      final session = InMemorySessionRepository(
        initialAccessToken: 'token-de-prueba',
      );
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      await tester.pumpWidget(routerHarness(session, identity));
      await tester.pumpAndSettle();

      expect(find.text('Mi perfil'), findsOneWidget);
      expect(find.byKey(const Key('biometric-switch')), findsOneWidget);
    });

    testWidgets('B6: el icono del home tiene target tactil >=48px',
        (tester) async {
      final session = InMemorySessionRepository(
        initialAccessToken: 'token-de-prueba',
      );
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      await tester.pumpWidget(
        routerHarness(session, identity, initial: '/home'),
      );
      await tester.pumpAndSettle();

      // Sin el fix el boton vivia en un circulo de 40px; ahora el area
      // tactil es de 48px con el mismo circulo visual de 40px.
      final size = tester.getSize(find.byKey(const Key('homeProfile')));
      expect(size.width, greaterThanOrEqualTo(48));
      expect(size.height, greaterThanOrEqualTo(48));
    });

    testWidgets('B1: el volver del topbar hace pop sin romper (cromo)',
        (tester) async {
      final session = InMemorySessionRepository(
        initialAccessToken: 'token-de-prueba',
      );
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      await tester.pumpWidget(
        routerHarness(session, identity, initial: '/home'),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('homeProfile')));
      await tester.pumpAndSettle();
      expect(find.text('Mi perfil'), findsOneWidget);

      // El "volver" es cromo navegable (fuera de CA-06 visual): hace `pop`
      // y deja el home intacto, sin excepciones.
      await tester.tap(find.byKey(const Key('profile-back')));
      await tester.pumpAndSettle();

      expect(find.text('Mi perfil'), findsNothing);
      expect(find.byKey(const Key('homeProfile')), findsOneWidget);
    });

    testWidgets('el icono del home navega a /profile', (tester) async {
      final session = InMemorySessionRepository(
        initialAccessToken: 'token-de-prueba',
      );
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      await tester.pumpWidget(
        routerHarness(session, identity, initial: '/home'),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('homeProfile')), findsOneWidget);
      await tester.tap(find.byKey(const Key('homeProfile')));
      await tester.pumpAndSettle();

      expect(find.text('Mi perfil'), findsOneWidget);
      expect(find.text('Ingreso por biometría'), findsOneWidget);
    });

    testWidgets('logout desde /profile deja la sesion y sale al login',
        (tester) async {
      debugDisableLoginAutoTick = true;
      addTearDown(() => debugDisableLoginAutoTick = false);
      final session = InMemorySessionRepository(
        initialAccessToken: 'token-de-prueba',
      );
      final identity = InMemorySessionIdentityStore(userRef: 'u-1');
      addTearDown(identity.dispose);
      // `onLogout` replica `SessionService.logout()`: limpia lo local; la
      // guarda reacciona al `ChangeNotifier` de la sesion.
      await tester.pumpWidget(
        routerHarness(
          session,
          identity,
          onLogout: () => session.clearOnLogout(),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Mi perfil'), findsOneWidget);

      await tester.scrollUntilVisible(
        find.byKey(const Key('profile-logout')),
        300,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('profile-logout')));
      await tester.pumpAndSettle();

      expect(session.isAuthenticated, isFalse);
      expect(find.text('Mi perfil'), findsNothing);
      expect(find.text('Inicia sesión'), findsOneWidget);
    });
  });
}
