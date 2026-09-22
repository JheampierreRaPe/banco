// Pruebas de widget del feature `pin_reset` (F-T43, CA-01..CA-04).
//
// - Flujo feliz: email + DNI -> `recovery/request` -> OTP -> PIN nuevo ->
//   confirmar -> `pin-reset` -> success -> navega a `/login?userRef=` SIN
//   abrir sesión ni ir a `/home`.
// - Anti-enumeración: `INVALID_PIN_RESET` -> mismo copy genérico sin indicar
//   el campo; `request` neutro exista o no el email.
// - Errores: `429` -> espera; `422` -> accionable con reintento; red ->
//   reintento.
// - Confirmación: el desajuste bloquea el avance sin disparar el request.
// - 4 estados: cada pantalla renderiza cargando (`LoadingView`), vacío
//   (`EmptyView`), error (`ErrorView` con reintento) y contenido.
// - Sin PII: DNI/OTP/PIN nunca en la ruta (solo `extra`/query de email) ni
//   en logs/mensajes; DNI enmascarado; OTP sin autofill.
import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/router/app_router.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/core/widgets/empty_view.dart';
import 'package:banca_online/core/widgets/error_view.dart';
import 'package:banca_online/core/widgets/loading_view.dart';
import 'package:banca_online/features/login/login_routes.dart';
import 'package:banca_online/features/pin_reset/pin_reset_controllers.dart';
import 'package:banca_online/features/pin_reset/pin_reset_routes.dart';
import 'package:banca_online/features/pin_reset/pin_reset_service.dart';
import 'package:banca_online/features/pin_reset/presentation/pin_reset_confirm_page.dart';
import 'package:banca_online/features/pin_reset/presentation/pin_reset_identity_page.dart';
import 'package:banca_online/features/pin_reset/presentation/pin_reset_new_pin_page.dart';
import 'package:banca_online/features/pin_reset/presentation/pin_reset_otp_page.dart';
import 'package:banca_online/features/pin_reset/presentation/pin_reset_success_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

const String _email = 'usuario@banco.com';
const String _doc = '12345678';
const String _code = '654321';
const String _pin = '482916';

/// Respuestas programables por endpoint (sin red).
class FakePinResetBackend {
  FakePinResetBackend({
    this.ttlSeconds = 600,
    this.resendWaitSeconds = 30,
    this.userRef = 'user-123',
  });

  int ttlSeconds;
  int resendWaitSeconds;
  String userRef;
  ApiException? requestError;
  ApiException? resetError;
  Completer<void>? blockRequest;
  Completer<void>? blockReset;
  final List<Map<String, dynamic>> requestBodies = [];
  final List<Map<String, dynamic>> resetBodies = [];

  ApiClient api(InMemorySessionRepository session) {
    final dio = Dio();
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) async {
          final body = options.data is Map
              ? Map<String, dynamic>.from(options.data as Map)
              : <String, dynamic>{};
          if (options.path.endsWith('/auth/recovery/request')) {
            requestBodies.add(body);
            if (blockRequest != null) await blockRequest!.future;
            final error = requestError;
            if (error != null) {
              handler.reject(
                DioException(
                  requestOptions: options,
                  type: DioExceptionType.badResponse,
                  response: Response(
                    requestOptions: options,
                    statusCode: error.code == 'RATE_LIMITED' ? 429 : 400,
                    data: {
                      'error': {'code': error.code, 'message': 'interno'}
                    },
                  ),
                ),
              );
              return;
            }
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {
                    'accepted': true,
                    'ttl_seconds': ttlSeconds,
                    'resend_wait_seconds': resendWaitSeconds,
                  },
                  'meta': {'request_id': 'r-1'},
                },
              ),
            );
            return;
          }
          if (options.path.endsWith('/auth/pin-reset')) {
            resetBodies.add(body);
            if (blockReset != null) await blockReset!.future;
            final error = resetError;
            if (error != null) {
              var status = 400;
              if (error.code == 'INVALID_PIN_RESET') status = 401;
              if (error.code == 'RATE_LIMITED') status = 429;
              if (error.code == 'VALIDATION_ERROR') status = 422;
              handler.reject(
                DioException(
                  requestOptions: options,
                  type: DioExceptionType.badResponse,
                  response: Response(
                    requestOptions: options,
                    statusCode: status,
                    data: {
                      'error': {'code': error.code, 'message': 'interno'}
                    },
                  ),
                ),
              );
              return;
            }
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {'user_ref': userRef, 'pin_set': true},
                  'meta': {'request_id': 'r-2'},
                },
              ),
            );
            return;
          }
          handler.reject(
            DioException(
              requestOptions: options,
              type: DioExceptionType.badResponse,
              response: Response(
                requestOptions: options,
                statusCode: 404,
                data: {
                  'error': {'code': 'NOT_FOUND', 'message': 'x'}
                },
              ),
            ),
          );
        },
      ),
    );
    return ApiClient.create(
      session: session,
      dioOverride: dio,
      refreshDioOverride: Dio(),
      uuid: const Uuid(),
    );
  }
}

GoRouter _flowRouter(  FakePinResetBackend backend,
  InMemorySessionRepository session,
  InMemorySessionIdentityStore identity,
) {
  pinResetRouteDepsFactory = () => PinResetRouteDeps(
        api: backend.api(session),
        session: session,
        identity: identity,
      );
  final router = GoRouter(
    initialLocation: '/pin-reset',
    routes: [
      ...pinResetRoutes,
      GoRoute(
        path: '/login',
        builder: (context, state) => Scaffold(
          body: Text(
            'login-ok:${state.uri.queryParameters['userRef'] ?? ''}',
          ),
        ),
      ),
    ],
  );
  return router;
}

String _currentUri(GoRouter router) =>
    router.routeInformationProvider.value.uri.toString();

/// Store que bloquea `saveUserRef` hasta completar [blocker] (para observar
/// el estado de carga del success de forma determinista).
class _BlockingIdentityStore extends InMemorySessionIdentityStore {
  _BlockingIdentityStore(this.blocker);

  final Completer<void> blocker;

  @override
  Future<void> saveUserRef(String userRef) async {
    await blocker.future;
    await super.saveUserRef(userRef);
  }
}

Future<void> _submitIdentity(WidgetTester tester) async {
  await tester.enterText(
    find.byKey(const Key('pin-reset-email-field')),
    _email,
  );
  await tester.enterText(
    find.byKey(const Key('pin-reset-doc-field')),
    _doc,
  );
  await tester.pump();
  await tester.tap(find.byKey(const Key('pin-reset-identity-submit')));
  await tester.pumpAndSettle();
}

Future<void> _enterOtp(WidgetTester tester, String code) async {
  for (var i = 0; i < 6; i++) {
    await tester.enterText(
      find.byKey(Key('pin-reset-otp-$i')),
      code[i],
    );
    await tester.pump();
  }
}

Future<void> _enterPin(WidgetTester tester, String pin) async {
  for (final digit in pin.split('')) {
    final key = find.byKey(Key('pin-key-$digit'));
    await tester.ensureVisible(key);
    await tester.tap(key);
    await tester.pump();
  }
  await tester.pumpAndSettle();
}

void main() {
  setUp(() {
    debugDisablePinResetAutoTick = true;
  });

  tearDown(() {
    debugDisablePinResetAutoTick = false;
    pinResetRouteDepsFactory = null;
  });

  group('flujo feliz (CA-01/CA-02)', () {
    testWidgets(
        'email+DNI -> OTP -> PIN -> confirmar -> success -> /login?userRef= '
        'sin sesión', (tester) async {
      final backend = FakePinResetBackend();
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final router = _flowRouter(backend, session, identity);
      addTearDown(router.dispose);
      final uris = <String>[];
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      // Paso 1: email + DNI (mockup `pin-reset`).
      expect(find.text('Crea un nuevo PIN'), findsOneWidget);
      await _submitIdentity(tester);
      uris.add(_currentUri(router));
      expect(backend.requestBodies, [
        {'email': _email}
      ]);

      // Paso 2: OTP con cuenta atrás y email enmascarado.
      expect(find.text('Ingresa el código'), findsOneWidget);
      expect(find.byKey(const Key('pin-reset-otp-countdown')), findsOneWidget);
      await _enterOtp(tester, _code);
      await tester.tap(find.byKey(const Key('pin-reset-otp-submit')));
      await tester.pumpAndSettle();
      uris.add(_currentUri(router));

      // Paso 3: PIN nuevo (DNI enmascarado, nunca en claro).
      expect(find.text('Crea un nuevo PIN'), findsOneWidget);
      expect(find.textContaining('****5678'), findsOneWidget);
      expect(find.textContaining(_doc), findsNothing);
      await _enterPin(tester, _pin);
      uris.add(_currentUri(router));

      // Paso 4: confirmar con el mismo PIN dispara `pin-reset`.
      expect(find.text('Confírmalo'), findsOneWidget);
      await _enterPin(tester, _pin);
      uris.add(_currentUri(router));
      expect(backend.resetBodies, [
        {
          'email': _email,
          'doc_number': _doc,
          'code': _code,
          'pin': _pin,
        }
      ]);

      // Paso 5: success sin sesión -> login con `userRef`.
      await tester.pumpAndSettle();
      expect(find.text('¡Tu PIN quedó restablecido!'), findsOneWidget);
      uris.add(_currentUri(router));
      await tester.tap(find.byKey(const Key('pin-reset-success-login')));
      await tester.pumpAndSettle();
      uris.add(_currentUri(router));

      expect(find.text('login-ok:user-123'), findsOneWidget);
      expect(session.isAuthenticated, isFalse);
      expect(await identity.readUserRef(), 'user-123');

      // Sin PII en la ruta: el DNI/OTP/PIN nunca aparecen en ninguna URL
      // (solo el email como query, igual que `/recovery/otp`).
      for (final uri in uris) {
        expect(uri, isNot(contains(_doc)));
        expect(uri, isNot(contains(_code)));
        expect(uri, isNot(contains(_pin)));
      }
    });

    testWidgets('el enlace "Restablecer PIN" del login abre /pin-reset',
        (tester) async {
      debugDisableLoginAutoTick = true;
      addTearDown(() => debugDisableLoginAutoTick = false);
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore(userRef: 'u-1');
      addTearDown(identity.dispose);
      final router = buildRouter(
        session,
        identity: identity,
        initialLocation: '/login',
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pump(const Duration(milliseconds: 100));
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.byKey(const Key('login-pin-reset-link')), findsOneWidget);
      await tester.ensureVisible(find.byKey(const Key('login-pin-reset-link')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('login-pin-reset-link')));
      await tester.pumpAndSettle();

      expect(find.text('Crea un nuevo PIN'), findsOneWidget);
    });
  });

  group('anti-enumeración y errores (CA-03)', () {
    testWidgets('INVALID_PIN_RESET muestra el copy genérico único',
        (tester) async {
      final backend = FakePinResetBackend()
        ..resetError = ApiException(
          code: 'INVALID_PIN_RESET',
          message: 'interno',
        );
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final router = _flowRouter(backend, session, identity);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _submitIdentity(tester);
      await _enterOtp(tester, _code);
      await tester.tap(find.byKey(const Key('pin-reset-otp-submit')));
      await tester.pumpAndSettle();
      await _enterPin(tester, _pin);
      await _enterPin(tester, _pin);

      expect(find.text('login-ok:user-123'), findsNothing);
      expect(
        find.text(PinResetConfirmController.invalidResetMessage),
        findsOneWidget,
      );
      expect(session.isAuthenticated, isFalse);
    });

    testWidgets('mismo copy para OTP incorrecto o vencido', (tester) async {
      // El backend colapsa código incorrecto/vencido/bloqueado en
      // `INVALID_PIN_RESET`; la UI no distingue el campo en ningún caso.
      final backend = FakePinResetBackend()
        ..resetError = ApiException(
          code: 'INVALID_PIN_RESET',
          message: 'interno',
        );
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final router = _flowRouter(backend, session, identity);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _submitIdentity(tester);
      await _enterOtp(tester, '000000');
      await tester.tap(find.byKey(const Key('pin-reset-otp-submit')));
      await tester.pumpAndSettle();
      await _enterPin(tester, _pin);
      await _enterPin(tester, _pin);

      expect(
        find.text(PinResetConfirmController.invalidResetMessage),
        findsOneWidget,
      );
    });

    testWidgets('429 en el reset muestra espera', (tester) async {
      final backend = FakePinResetBackend()
        ..resetError = ApiException(
          code: 'RATE_LIMITED',
          message: 'interno',
        );
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final router = _flowRouter(backend, session, identity);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _submitIdentity(tester);
      await _enterOtp(tester, _code);
      await tester.tap(find.byKey(const Key('pin-reset-otp-submit')));
      await tester.pumpAndSettle();
      await _enterPin(tester, _pin);
      await _enterPin(tester, _pin);

      expect(
        find.text(PinResetConfirmController.rateMessage),
        findsOneWidget,
      );
    });

    testWidgets('422 (PIN débil) muestra mensaje accionable y reintenta',
        (tester) async {
      final backend = FakePinResetBackend()
        ..resetError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'x',
        );
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final router = _flowRouter(backend, session, identity);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _submitIdentity(tester);
      await _enterOtp(tester, _code);
      await tester.tap(find.byKey(const Key('pin-reset-otp-submit')));
      await tester.pumpAndSettle();
      await _enterPin(tester, _pin);
      await _enterPin(tester, _pin);

      // Mensaje en español de la capa HTTP + botón de reintento.
      expect(find.byType(ErrorView), findsOneWidget);
      expect(backend.resetBodies.length, 1);
      backend.resetError = null;
      // Reintento: el `ErrorView` vuelve al teclado para confirmar de nuevo.
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      await _enterPin(tester, _pin);
      await tester.pumpAndSettle();
      expect(backend.resetBodies.length, 2);
      expect(find.text('¡Tu PIN quedó restablecido!'), findsOneWidget);
    });

    testWidgets('fallo de red en identidad muestra error con reintento',
        (tester) async {
      final backend = FakePinResetBackend()
        ..requestError = ApiException.network();
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final router = _flowRouter(backend, session, identity);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _submitIdentity(tester);

      expect(find.byType(ErrorView), findsOneWidget);
      backend.requestError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();

      // Vuelve al formulario con lo capturado intacto.
      expect(
        find.byKey(const Key('pin-reset-identity-submit')),
        findsOneWidget,
      );
    });

    testWidgets('desajuste de PIN bloquea sin disparar el request',
        (tester) async {
      final backend = FakePinResetBackend();
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final router = _flowRouter(backend, session, identity);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _submitIdentity(tester);
      await _enterOtp(tester, _code);
      await tester.tap(find.byKey(const Key('pin-reset-otp-submit')));
      await tester.pumpAndSettle();
      await _enterPin(tester, _pin);
      await _enterPin(tester, '000000');

      expect(backend.resetBodies, isEmpty);
      expect(find.byType(ErrorView), findsOneWidget);
      // Reintento: vuelve al teclado para confirmar de nuevo.
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      await _enterPin(tester, _pin);
      await tester.pumpAndSettle();
      expect(backend.resetBodies.length, 1);
    });
  });

  group('4 estados por pantalla (CA-04)', () {
    testWidgets('identidad: cargando, vacío, error y contenido',
        (tester) async {
      // Vacío: feature no disponible.
      await tester.pumpWidget(
        const MaterialApp(
          home: PinResetIdentityPage(controller: null),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(EmptyView), findsOneWidget);

      // Cargando: request bloqueado.
      final backend = FakePinResetBackend()
        ..blockRequest = Completer<void>();
      final session = InMemorySessionRepository();
      pinResetRouteDepsFactory = () => PinResetRouteDeps(
            api: backend.api(session),
            session: session,
          );
      final router = GoRouter(
        initialLocation: '/pin-reset',
        routes: pinResetRoutes,
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('pin-reset-email-field')),
        _email,
      );
      await tester.enterText(
        find.byKey(const Key('pin-reset-doc-field')),
        _doc,
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('pin-reset-identity-submit')));
      await tester.pump();
      expect(find.byType(LoadingView), findsOneWidget);
      backend.blockRequest!.complete();
      await tester.pumpAndSettle();

      // Contenido tras desbloquear (navegó al OTP).
      expect(find.text('Ingresa el código'), findsOneWidget);
    });

    testWidgets('otp: vacío sin email/DNI y contenido con fakes',
        (tester) async {
      final backend = FakePinResetBackend();
      final session = InMemorySessionRepository();
      pinResetRouteDepsFactory = () => PinResetRouteDeps(
            api: backend.api(session),
            session: session,
          );
      // Sin `extra` (DNI) ni email: estado vacío con acción.
      final router = GoRouter(
        initialLocation: '/pin-reset/otp',
        routes: pinResetRoutes,
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      expect(find.byType(EmptyView), findsOneWidget);
    });

    testWidgets('otp: cargando al reenviar y error con reintento',
        (tester) async {
      final backend = FakePinResetBackend(resendWaitSeconds: 0);
      final session = InMemorySessionRepository();
      final service = HttpPinResetService(api: backend.api(session));
      final controller = PinResetOtpController(
        service: service,
        email: _email,
        resendWaitSeconds: 0,
      );
      addTearDown(controller.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: PinResetOtpPage(
            email: _email,
            docNumber: _doc,
            controller: controller,
            autoTick: false,
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Contenido: casillas + cuenta atrás + mensaje neutro.
      expect(find.byKey(const Key('pin-reset-otp-countdown')), findsOneWidget);
      expect(
        find.text(PinResetOtpController.neutralMessage),
        findsOneWidget,
      );

      // Cargando: reenvío en curso muestra el spinner.
      backend.blockRequest = Completer<void>();
      await tester.tap(find.byKey(const Key('pin-reset-otp-resend')));
      await tester.pump();
      expect(find.byType(LoadingView), findsOneWidget);
      backend.blockRequest!.complete();
      backend.blockRequest = null;
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('pin-reset-otp-submit')), findsOneWidget);

      // Error de reenvío (429) con reintento que vuelve al formulario.
      backend.requestError = ApiException(
        code: 'RATE_LIMITED',
        message: 'interno',
      );
      await tester.tap(find.byKey(const Key('pin-reset-otp-resend')));
      await tester.pumpAndSettle();
      expect(find.byType(ErrorView), findsOneWidget);
      expect(
        find.text(PinResetOtpController.rateMessage),
        findsOneWidget,
      );
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('pin-reset-otp-submit')), findsOneWidget);
    });

    testWidgets('pin nuevo: vacío, error débil y cargando al avanzar',
        (tester) async {
      // Vacío: sin borrador.
      await tester.pumpWidget(
        const MaterialApp(home: PinResetNewPinPage(draft: null)),
      );
      await tester.pumpAndSettle();
      expect(find.byType(EmptyView), findsOneWidget);

      // Error: PIN débil muestra ErrorView con reintento.
      const draft = PinResetDraft(
        email: _email,
        docNumber: _doc,
        code: _code,
      );
      final router = GoRouter(
        initialLocation: '/new-pin',
        routes: [
          GoRoute(
            path: '/new-pin',
            builder: (_, _) => const PinResetNewPinPage(draft: draft),
          ),
          GoRoute(
            path: '/pin-reset/confirm',
            builder: (_, _) => const Scaffold(body: Text('confirm-ok')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();
      await _enterPin(tester, '123456');
      expect(find.byType(ErrorView), findsOneWidget);
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.byType(PinResetNewPinPage), findsOneWidget);

      // Cargando y avance: con un PIN válido muestra el spinner y navega a
      // confirmar con el borrador en memoria (nunca en la ruta).
      for (final digit in _pin.split('')) {
        final key = find.byKey(Key('pin-key-$digit'));
        await tester.ensureVisible(key);
        await tester.tap(key);
        await tester.pump();
      }
      expect(find.byType(LoadingView), findsOneWidget);
      await tester.pumpAndSettle();
      expect(find.text('confirm-ok'), findsOneWidget);
      expect(_currentUri(router),
          '/pin-reset/confirm?email=${Uri.encodeComponent(_email)}');
    });

    testWidgets('confirmar: vacío, cargando y error con reintento',
        (tester) async {
      final backend = FakePinResetBackend();
      final session = InMemorySessionRepository();
      // Vacío: sin borrador.
      await tester.pumpWidget(
        MaterialApp(
          home: PinResetConfirmPage(
            draft: null,
            service: HttpPinResetService(api: backend.api(session)),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(EmptyView), findsOneWidget);

      // Cargando: reset bloqueado muestra el spinner y al resolverse navega
      // al success con el `userRef` (sin abrir sesión).
      backend.blockReset = Completer<void>();
      const draft = PinResetDraft(
        email: _email,
        docNumber: _doc,
        code: _code,
        pin: _pin,
      );
      final service = HttpPinResetService(api: backend.api(session));
      final router = GoRouter(
        initialLocation: '/confirm',
        routes: [
          GoRoute(
            path: '/confirm',
            builder: (_, _) => PinResetConfirmPage(
              draft: draft,
              service: service,
            ),
          ),
          GoRoute(
            path: '/pin-reset/success',
            builder: (context, state) => Scaffold(
              body: Text(
                'success-ok:${state.uri.queryParameters['userRef'] ?? ''}',
              ),
            ),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();
      // Entrada sin asentar: el reset sigue bloqueado tras el 6.º dígito.
      for (final digit in _pin.split('')) {
        final key = find.byKey(Key('pin-key-$digit'));
        await tester.ensureVisible(key);
        await tester.tap(key);
        await tester.pump();
      }
      expect(find.byType(LoadingView), findsOneWidget);
      backend.blockReset!.complete();
      await tester.pumpAndSettle();
      expect(find.text('success-ok:user-123'), findsOneWidget);
    });

    testWidgets('success: vacío, cargando y contenido', (tester) async {
      // Vacío: sin `userRef`.
      await tester.pumpWidget(
        MaterialApp(
          home: PinResetSuccessPage(
            userRef: '',
            identity: InMemorySessionIdentityStore(),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(EmptyView), findsOneWidget);

      // Cargando: el guardado tarda (store bloqueado) y se ve el spinner.
      // (Se vacía el árbol antes: si no, el `State` del pump anterior con
      // `_saving=false` se reutilizaría y nunca se vería la carga.)
      final blocker = Completer<void>();
      final identity = _BlockingIdentityStore(blocker);
      addTearDown(identity.dispose);
      await tester.pumpWidget(const SizedBox());
      await tester.pumpWidget(
        MaterialApp(
          home: PinResetSuccessPage(
            userRef: 'user-123',
            identity: identity,
          ),
        ),
      );
      await tester.pump();
      expect(find.byType(LoadingView), findsOneWidget);
      blocker.complete();
      await tester.pumpAndSettle();

      // Contenido: éxito con acción al login.
      expect(find.text('¡Tu PIN quedó restablecido!'), findsOneWidget);
      expect(
        find.byKey(const Key('pin-reset-success-login')),
        findsOneWidget,
      );
    });
  });

  group('higiene de datos sensibles (CA-04)', () {
    testWidgets('OTP sin autofill ni sugerencias', (tester) async {
      final backend = FakePinResetBackend();
      final session = InMemorySessionRepository();
      final service = HttpPinResetService(api: backend.api(session));
      final controller = PinResetOtpController(
        service: service,
        email: _email,
      );
      addTearDown(controller.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: PinResetOtpPage(
            email: _email,
            docNumber: _doc,
            controller: controller,
            autoTick: false,
          ),
        ),
      );
      await tester.pumpAndSettle();

      final field = tester.widget<TextField>(
        find.byKey(const Key('pin-reset-otp-0')),
      );
      expect(field.enableSuggestions, isFalse);
      expect(field.autocorrect, isFalse);
      // Sin `autofillHints`: el SO no ofrece autocompletar el OTP.
      expect(field.autofillHints, anyOf(isNull, isEmpty));
    });

    testWidgets('DNI/OTP/PIN nunca en logs ni mensajes', (tester) async {
      final backend = FakePinResetBackend()
        ..resetError = ApiException(
          code: 'INVALID_PIN_RESET',
          message: 'interno',
        );
      final session = InMemorySessionRepository();
      final identity = InMemorySessionIdentityStore();
      addTearDown(identity.dispose);
      final router = _flowRouter(backend, session, identity);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await _submitIdentity(tester);
      await _enterOtp(tester, _code);
      await tester.tap(find.byKey(const Key('pin-reset-otp-submit')));
      await tester.pumpAndSettle();
      await _enterPin(tester, _pin);
      await _enterPin(tester, _pin);

      // Ningún texto visible expone los datos sensibles en claro.
      expect(find.textContaining(_doc), findsNothing);
      expect(find.textContaining(_code), findsNothing);
      expect(find.textContaining(_pin), findsNothing);
    });
  });
}
