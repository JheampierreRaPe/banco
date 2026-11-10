// Pruebas E2-T21 (HU08): reto biometrico en el flujo de pago + 6 estados.
//
// - Camino feliz: `PENDING_AUTHORIZATION` -> reto -> firma -> continuacion.
// - Fallo/cancelacion biometrica -> fallback a PIN (sin llamar a red).
// - `EXPIRED_NONCE` -> rotacion (`POST .../authorize/challenge`) + reintento
//   con `nonce` nuevo y `Idempotency-Key` NUEVA (la anterior snapshotteo el
//   fallo: misma clave + cuerpo distinto = `409`).
// - Reintento usa clave nueva derivada (controlador y servicio).
// - Regresion realista: fake con contrato real del backend (nonce con TTL +
//   idempotencia con snapshot/409) donde el comportamiento previo (misma
//   clave + `GET` sin rotacion) nunca autoriza.
// - Los 6 estados de error renderizan titulo + accion primaria.
// - Integracion `ConfirmPage`: reto exitoso navega al comprobante; fallo
//   biometrico muestra el fallback a PIN.
library;

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/payment_authorization/data/payment_authorization_service.dart';
import 'package:banca_online/features/payment_authorization/payment_authorization_controller.dart';
import 'package:banca_online/features/payment_authorization/payment_authorization_routes.dart';
import 'package:banca_online/features/payment_authorization/presentation/biometric_challenge_sheet.dart';
import 'package:banca_online/features/payment_authorization/presentation/payment_error_page.dart';
import 'package:banca_online/features/transfers/data/transfers_service.dart';
import 'package:banca_online/features/transfers/models/resolve_target.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/transfers/presentation/confirm_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del servicio de autorizacion: guion por llamada + registro de
/// claves para probar idempotencia (clave nueva en el reintento).
class FakePaymentAuthService implements PaymentAuthorizationServiceBase {
  /// Guion de `authorize`: cada llamada consume un item
  /// ([AuthorizationResult] o [ApiException]).
  List<Object> authorizeScript = const [];
  int authorizeCalls = 0;

  /// Claves recibidas en cada llamada a `authorize`, en orden.
  List<String?> authorizeKeys = [];

  /// Nonces recibidos en cada llamada a `authorize`, en orden.
  List<String> authorizeNonces = [];

  /// Respuesta de `refreshChallenge` (rotacion tras `EXPIRED_NONCE`).
  TransactionDetail refreshResult = const TransactionDetail(
    transactionId: 'tx-1',
    type: 'OWN_TRANSFER',
    status: 'PENDING_AUTHORIZATION',
    amountMinor: 25000,
    currency: 'PEN',
    nonce: 'fresh-nonce',
  );
  ApiException? refreshError;
  int refreshCalls = 0;

  /// Guion de `authorizeWithPin` (E2-T31): cada llamada consume un item
  /// ([AuthorizationResult] o [ApiException]).
  List<Object> authorizePinScript = const [];
  int authorizePinCalls = 0;

  /// PINs recibidos en cada llamada a `authorizeWithPin`, en orden.
  List<String> authorizePins = [];

  /// Claves recibidas en cada llamada a `authorizeWithPin`, en orden.
  List<String?> authorizePinKeys = [];

  AuthorizationResult _success() =>
      const AuthorizationResult(transactionId: 'tx-1', status: 'SETTLED');

  @override
  Future<AuthorizationResult> authorize({
    required String transactionId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  }) async {
    final i = authorizeCalls;
    authorizeCalls++;
    authorizeKeys.add(idempotencyKey);
    authorizeNonces.add(nonce);
    final item = i < authorizeScript.length
        ? authorizeScript[i]
        : authorizeScript.isEmpty
        ? _success()
        : authorizeScript.last;
    if (item is ApiException) throw item;
    return item as AuthorizationResult;
  }

  @override
  Future<AuthorizationResult> authorizeWithPin({
    required String transactionId,
    required String pin,
    String? idempotencyKey,
  }) async {
    final i = authorizePinCalls;
    authorizePinCalls++;
    authorizePins.add(pin);
    authorizePinKeys.add(idempotencyKey);
    final item = i < authorizePinScript.length
        ? authorizePinScript[i]
        : authorizePinScript.isEmpty
        ? _success()
        : authorizePinScript.last;
    if (item is ApiException) throw item;
    return item as AuthorizationResult;
  }

  @override
  Future<TransactionDetail> refreshChallenge(String transactionId) async {
    refreshCalls++;
    if (refreshError != null) throw refreshError!;
    return refreshResult;
  }
}

PaymentAuthorizationController _controller({
  required FakePaymentAuthService service,
  BiometricReader? reader,
  String deviceId = 'dev-1',
}) {
  return PaymentAuthorizationController(
    service: service,
    reader: reader ?? FakeBiometricReader(),
    signNonce: (nonce) async => 'sig-of-$nonce',
    deviceId: deviceId,
  );
}

/// Fake con el contrato REAL del backend (regresion `EXPIRED_NONCE`).
///
/// Modela lo que el backend hace de verdad (no un guion arbitrario):
/// - El `nonce` almacenado tiene TTL: vencido o distinto al vigente ->
///   `EXPIRED_NONCE`, y el fallo queda snapshotteado bajo su
///   `Idempotency-Key` (OBS-3).
/// - Misma clave + mismo cuerpo -> replay del snapshot (incluido el error).
/// - Misma clave + cuerpo distinto -> `409 CONFLICT`.
/// - `refreshChallenge` rota el reto (`POST .../authorize/challenge`): el
///   `nonce` nuevo es distinto al almacenado (el `GET` anterior devolvia el
///   mismo `nonce` sin rotar).
///
/// Con el comportamiento previo (reintento con la MISMA clave y `GET` sin
/// rotacion) este flujo NUNCA autoriza: misma clave + mismo cuerpo repite
/// el `EXPIRED_NONCE` snapshotteado, y misma clave + cuerpo distinto da
/// `409`. Solo rota + clave nueva autoriza.
class RealBackendFake implements PaymentAuthorizationServiceBase {
  String storedNonce = 'n-old';
  bool storedExpired = true;

  final Map<String, Map<String, Object>> _snapshots = {};
  List<String?> authorizeKeys = [];
  List<String> authorizeNonces = [];
  int authorizeCalls = 0;
  int refreshCalls = 0;

  String _bodyOf(
    String transactionId,
    String nonce,
    String signature,
    String deviceId,
  ) =>
      'authorize|$transactionId|$nonce|$signature|$deviceId';

  @override
  Future<AuthorizationResult> authorize({
    required String transactionId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  }) async {
    authorizeCalls++;
    authorizeKeys.add(idempotencyKey);
    authorizeNonces.add(nonce);
    final key = idempotencyKey ?? '';
    final body = _bodyOf(transactionId, nonce, signature, deviceId);
    final snap = _snapshots[key];
    if (snap != null) {
      if (snap['body'] != body) {
        throw ApiException(
          code: 'CONFLICT',
          message: 'Idempotency-Key ya usada con un cuerpo distinto.',
        );
      }
      final error = snap['error'];
      if (error is ApiException) throw error;
      return snap['response'] as AuthorizationResult;
    }
    if (storedExpired || nonce != storedNonce) {
      final error = ApiException(
        code: 'EXPIRED_NONCE',
        message: 'Desafio vencido, solicite uno nuevo.',
      );
      _snapshots[key] = {'body': body, 'error': error};
      throw error;
    }
    final ok = AuthorizationResult(
      transactionId: transactionId,
      status: 'SETTLED',
    );
    _snapshots[key] = {'body': body, 'response': ok};
    return ok;
  }

  @override
  Future<AuthorizationResult> authorizeWithPin({
    required String transactionId,
    required String pin,
    String? idempotencyKey,
  }) async {
    final key = idempotencyKey ?? '';
    final body = 'authorize-pin|$transactionId|$pin';
    final snap = _snapshots[key];
    if (snap != null) {
      if (snap['body'] != body) {
        throw ApiException(
          code: 'CONFLICT',
          message: 'Idempotency-Key ya usada con un cuerpo distinto.',
        );
      }
      final error = snap['error'];
      if (error is ApiException) throw error;
      return snap['response'] as AuthorizationResult;
    }
    final ok = AuthorizationResult(
      transactionId: transactionId,
      status: 'SETTLED',
    );
    _snapshots[key] = {'body': body, 'response': ok};
    return ok;
  }

  @override
  Future<TransactionDetail> refreshChallenge(String transactionId) async {
    refreshCalls++;
    storedNonce = 'n-fresh-1';
    storedExpired = false;
    return TransactionDetail(
      transactionId: transactionId,
      type: 'OWN_TRANSFER',
      status: 'PENDING_AUTHORIZATION',
      amountMinor: 25000,
      currency: 'PEN',
      nonce: storedNonce,
      expiresIn: 120,
    );
  }
}

PaymentAuthorizationController _realController(RealBackendFake service) {
  return PaymentAuthorizationController(
    service: service,
    reader: FakeBiometricReader(),
    signNonce: (nonce) async => 'sig-of-$nonce',
    deviceId: 'dev-1',
  );
}

void main() {
  group('PaymentAuthorizationController', () {
    test('reto exitoso autoriza y continua el flujo', () async {
      final service = FakePaymentAuthService();
      final c = _controller(service: service);

      final st = await c.authorize(
        transactionId: 'tx-1',
        nonce: 'n-1',
        idempotencyKey: 'k-1',
        reason: 'Autoriza',
      );

      expect(st.outcome, PaymentAuthOutcome.authorized);
      expect(st.result?.status, 'SETTLED');
      expect(st.attempts, 1);
      expect(service.authorizeCalls, 1);
      expect(service.authorizeKeys, ['k-1']);
      expect(service.authorizeNonces, ['n-1']);
    });

    test('biometria fallida no llama a red y pide fallback a PIN', () async {
      final service = FakePaymentAuthService();
      final c = _controller(
        service: service,
        reader: FakeBiometricReader(succeeds: false),
      );

      final st = await c.authorize(
        transactionId: 'tx-1',
        nonce: 'n-1',
        idempotencyKey: 'k-1',
        reason: 'Autoriza',
      );

      expect(st.outcome, PaymentAuthOutcome.requiresPinFallback);
      expect(service.authorizeCalls, 0);
    });

    test('sin biometria disponible va directo al fallback', () async {
      final service = FakePaymentAuthService();
      final c = _controller(
        service: service,
        reader: FakeBiometricReader(available: false),
      );

      final st = await c.authorize(
        transactionId: 'tx-1',
        nonce: 'n-1',
        idempotencyKey: 'k-1',
        reason: 'Autoriza',
      );

      expect(st.outcome, PaymentAuthOutcome.requiresPinFallback);
      expect(service.authorizeCalls, 0);
    });

    test(
      'EXPIRED_NONCE rota el reto y reintenta con clave nueva',
      () async {
        final service = FakePaymentAuthService()
          ..authorizeScript = [
            ApiException(code: 'EXPIRED_NONCE', message: 'Vencido.'),
            const AuthorizationResult(transactionId: 'tx-1', status: 'SETTLED'),
          ];
        final c = _controller(service: service);

        final st = await c.authorize(
          transactionId: 'tx-1',
          nonce: 'old-nonce',
          idempotencyKey: 'k-same',
          reason: 'Autoriza',
        );

        expect(st.outcome, PaymentAuthOutcome.authorized);
        expect(st.attempts, 2);
        expect(service.refreshCalls, 1);
        // Reintento con el `nonce` nuevo y una `Idempotency-Key` NUEVA: la
        // anterior snapshotteo el `EXPIRED_NONCE` (misma clave + cuerpo
        // distinto = `409` en el backend real).
        expect(service.authorizeKeys, ['k-same', 'k-same-retry-1']);
        expect(service.authorizeNonces, ['old-nonce', 'fresh-nonce']);
      },
    );

    test('doble EXPIRED_NONCE termina en expirado para reiniciar', () async {
      final service = FakePaymentAuthService()
        ..authorizeScript = [
          ApiException(code: 'EXPIRED_NONCE', message: 'Vencido.'),
          ApiException(code: 'EXPIRED_NONCE', message: 'Vencido.'),
        ];
      final c = _controller(service: service);

      final st = await c.authorize(
        transactionId: 'tx-1',
        nonce: 'old-nonce',
        idempotencyKey: 'k-1',
        reason: 'Autoriza',
      );

      expect(st.outcome, PaymentAuthOutcome.expired);
      expect(service.authorizeKeys, ['k-1', 'k-1-retry-1']);
    });

    test('firma invalida (NOT_AUTHORIZED) cae al fallback a PIN', () async {
      final service = FakePaymentAuthService()
        ..authorizeScript = [
          ApiException(code: 'NOT_AUTHORIZED', message: 'Invalida.'),
        ];
      final c = _controller(service: service);

      final st = await c.authorize(
        transactionId: 'tx-1',
        nonce: 'n-1',
        idempotencyKey: 'k-1',
        reason: 'Autoriza',
      );

      expect(st.outcome, PaymentAuthOutcome.requiresPinFallback);
      expect(st.message, isNotEmpty);
    });
  });

  group('EXPIRED_NONCE contra backend real (regresion)', () {
    test(
      'authorize -> EXPIRED_NONCE -> rotacion -> reintento autoriza',
      () async {
        final service = RealBackendFake();
        final c = _realController(service);

        final st = await c.authorize(
          transactionId: 'tx-1',
          nonce: 'n-old',
          idempotencyKey: 'k-1',
          reason: 'Autoriza',
        );

        expect(st.outcome, PaymentAuthOutcome.authorized);
        expect(st.result?.status, 'SETTLED');
        expect(st.attempts, 2);
        expect(service.refreshCalls, 1);
        expect(service.authorizeCalls, 2);
        // El `nonce` del reintento es el rotado, no el expirado.
        expect(service.authorizeNonces, ['n-old', 'n-fresh-1']);
        // El reintento usa clave NUEVA: con la anterior (misma clave +
        // cuerpo distinto) el backend real responde `409`, y con el mismo
        // cuerpo repite el `EXPIRED_NONCE` snapshotteado.
        expect(service.authorizeKeys.first, 'k-1');
        expect(service.authorizeKeys[1], isNot('k-1'));
        expect(service.authorizeKeys[1], 'k-1-retry-1');
      },
    );

    test('la clave de reintento deriva de la original', () {
      expect(paymentAuthRetryKey('k-1'), 'k-1-retry-1');
      expect(paymentAuthRetryKey(null), isNull);
      expect(paymentAuthRetryKey('  '), isNull);
    });

    test('la clave inicial deriva del borrador con sufijo -auth', () {
      expect(paymentAuthInitialKey('k-1'), 'k-1-auth');
      expect(paymentAuthInitialKey(null), isNull);
      expect(paymentAuthInitialKey('  '), isNull);
      // El reintento sobre la derivada da `<borrador>-auth-retry-1`.
      expect(
        paymentAuthRetryKey(paymentAuthInitialKey('k-1')),
        'k-1-auth-retry-1',
      );
    });

    test('la derivacion respeta el tope de 80 conservando el sufijo', () {
      final long = List.filled(80, 'k').join();
      final initial = paymentAuthInitialKey(long)!;
      expect(initial.length, 80);
      expect(initial.endsWith('-auth'), isTrue);
      final retry = paymentAuthRetryKey(initial)!;
      expect(retry.length, 80);
      expect(retry.endsWith('-retry-1'), isTrue);
    });

    test('detecta codigos de conflicto de idempotencia', () {
      expect(isPaymentAuthConflictCode('CONFLICT'), isTrue);
      expect(isPaymentAuthConflictCode('IDEMPOTENCY_CONFLICT'), isTrue);
      expect(isPaymentAuthConflictCode('DUPLICATE_REQUEST'), isTrue);
      expect(isPaymentAuthConflictCode('NOT_AUTHORIZED'), isFalse);
      expect(isPaymentAuthConflictCode(null), isFalse);
    });
  });

  group('Alcance de idempotencia transfer+authorize (causa raiz)', () {
    /// Servidor minimo con UQ GLOBAL `(key, user_id)` sin `endpoint`:
    /// la misma clave con cuerpo distinto da `409`, como el backend real
    /// (`uq_idempotency_key_user`). El fake anterior no lo modelaba y por
    /// eso el bug no se detecto.
    test('misma clave en transfer y authorize da 409', () async {
      final server = _SharedIdempotencyServer();
      // El `POST /transfers/third-party` reclama la clave del borrador.
      server.claim(
        key: 'k-draft',
        body: 'transfer|a1|b1|25000',
      );
      final auth = _SharedAuthFake(server: server);
      final c = PaymentAuthorizationController(
        service: auth,
        reader: FakeBiometricReader(),
        signNonce: (nonce) async => 'sig-of-$nonce',
        deviceId: 'dev-1',
      );
      // Reutilizar la clave del borrador en el `authorize` (cuerpo
      // distinto: firma/nonce) -> `409`, el reto nunca se ejecuta.
      final st = await c.authorize(
        transactionId: 'tx-1',
        nonce: 'n-1',
        idempotencyKey: 'k-draft',
        reason: 'Autoriza',
      );
      expect(st.outcome, PaymentAuthOutcome.failed);
      expect(st.code, 'CONFLICT');
    });

    test('clave derivada -auth autoriza sin conflicto', () async {
      final server = _SharedIdempotencyServer();
      server.claim(
        key: 'k-draft',
        body: 'transfer|a1|b1|25000',
      );
      final auth = _SharedAuthFake(server: server);
      final c = PaymentAuthorizationController(
        service: auth,
        reader: FakeBiometricReader(),
        signNonce: (nonce) async => 'sig-of-$nonce',
        deviceId: 'dev-1',
      );
      final derived = paymentAuthInitialKey('k-draft')!;
      expect(derived, 'k-draft-auth');
      final st = await c.authorize(
        transactionId: 'tx-1',
        nonce: 'n-1',
        idempotencyKey: derived,
        reason: 'Autoriza',
      );
      expect(st.outcome, PaymentAuthOutcome.authorized);
      expect(st.result?.status, 'SETTLED');
    });
  });

  group('PaymentErrorKind', () {
    test('mapea los codigos del backend a los 6 estados', () {
      expect(
        PaymentErrorKind.fromCode('INSUFFICIENT_FUNDS'),
        PaymentErrorKind.insufficientFunds,
      );
      expect(
        PaymentErrorKind.fromCode('DAILY_LIMIT_EXCEEDED'),
        PaymentErrorKind.dailyLimit,
      );
      expect(
        PaymentErrorKind.fromCode('BIOMETRIC_PREVENTIVE_BLOCK'),
        PaymentErrorKind.preventiveBlock,
      );
      expect(
        PaymentErrorKind.fromCode('EXPIRED_NONCE'),
        PaymentErrorKind.sessionExpired,
      );
      expect(
        PaymentErrorKind.fromCode('TRANSFER_TIMEOUT'),
        PaymentErrorKind.timeout,
      );
      expect(PaymentErrorKind.fromCode('CONFLICT'), PaymentErrorKind.duplicate);
      expect(
        PaymentErrorKind.fromCode('IDEMPOTENCY_CONFLICT'),
        PaymentErrorKind.duplicate,
      );
      expect(PaymentErrorKind.fromCode('whatever'), PaymentErrorKind.timeout);
    });
  });

  group('BiometricChallengeSheet (mockup 0:3964)', () {
    Future<void> pumpSheet(
      WidgetTester tester, {
      bool isAuthorizing = false,
      String errorMessage = '',
    }) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: BiometricChallengeSheet(
              amountLabel: 'S/ 250.00',
              recipientLabel: 'Mamá Lima',
              isAuthorizing: isAuthorizing,
              errorMessage: errorMessage,
            ),
          ),
        ),
      );
      // Con carga hay animacion infinita (`CircularProgressIndicator`):
      // un solo frame en vez de `pumpAndSettle`.
      if (isAuthorizing) {
        await tester.pump();
      } else {
        await tester.pumpAndSettle();
      }
    }

    testWidgets('muestra titulo, detalle y acciones del mockup', (
      tester,
    ) async {
      await pumpSheet(tester);

      expect(find.byKey(const Key('biometric-title')), findsOneWidget);
      expect(find.text('Autoriza la operación'), findsOneWidget);
      expect(
        find.text('Usa tu huella para enviar S/ 250.00 a Mamá Lima.'),
        findsOneWidget,
      );
      expect(find.byKey(const Key('biometric-pin-fallback')), findsOneWidget);
      expect(find.text('Usar mi PIN de seguridad'), findsOneWidget);
      expect(find.byKey(const Key('biometric-cancel')), findsOneWidget);
      expect(find.byKey(const Key('biometric-authorize')), findsOneWidget);
    });

    testWidgets('autorizando muestra carga y bloquea acciones', (tester) async {
      await pumpSheet(tester, isAuthorizing: true);

      expect(find.byKey(const Key('biometric-loading')), findsOneWidget);
      final auth = tester.widget<FilledButton>(
        find.byKey(const Key('biometric-authorize')),
      );
      expect(auth.enabled, isFalse);
    });

    testWidgets('fallo muestra mensaje claro con reintento', (tester) async {
      await pumpSheet(tester, errorMessage: 'No pudimos verificar tu huella.');

      expect(find.byKey(const Key('biometric-error')), findsOneWidget);
      expect(find.text('Reintentar con huella'), findsOneWidget);
    });
  });

  group('PaymentErrorView (mockups 1..6)', () {
    const cases = [
      (
        kind: PaymentErrorKind.insufficientFunds,
        title: 'No tienes saldo suficiente',
      ),
      (kind: PaymentErrorKind.dailyLimit, title: 'Superaste tu límite diario'),
      (
        kind: PaymentErrorKind.preventiveBlock,
        title: 'Acceso protegido por seguridad',
      ),
      (kind: PaymentErrorKind.sessionExpired, title: 'La solicitud expiró'),
      (kind: PaymentErrorKind.timeout, title: 'La operación tardó demasiado'),
      (kind: PaymentErrorKind.duplicate, title: 'Ya fue procesada'),
    ];

    for (final c in cases) {
      testWidgets('${c.title} renderiza con su accion primaria', (
        tester,
      ) async {
        // Superficie alta para que el `ListView` construya todo el contenido
        // (acciones + pie) sin quedar bajo el fold.
        tester.view.physicalSize = const Size(800, 2200);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(() {
          tester.view.resetPhysicalSize();
          tester.view.resetDevicePixelRatio();
        });
        var primary = false;
        var secondary = false;
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: PaymentErrorView(
                args: const PaymentErrorArgs(
                  kind: PaymentErrorKind.timeout,
                  recipientLabel: 'M. Rojas (Mamá Lima)',
                  bankMasked: 'BCP · •••• 7744',
                  amountLabel: 'S/ 350.00',
                  feeLabel: 'S/ 3.50',
                  totalLabel: 'S/ 353.50',
                ).copyWithKind(c.kind),
                onPrimary: () => primary = true,
                onSecondary: () => secondary = true,
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();

        expect(find.text(c.title), findsOneWidget);
        expect(find.byKey(const Key('payment-error-title')), findsOneWidget);
        expect(find.byKey(const Key('payment-error-primary')), findsOneWidget);
        expect(
          find.byKey(const Key('payment-error-secondary')),
          findsOneWidget,
        );
        expect(find.byKey(const Key('payment-error-tech')), findsOneWidget);
        expect(find.text('Transacción protegida CuyCash'), findsOneWidget);

        await tester.tap(find.byKey(const Key('payment-error-primary')));
        await tester.pump();
        expect(primary, isTrue);

        await tester.tap(find.byKey(const Key('payment-error-secondary')));
        await tester.pump();
        expect(secondary, isTrue);
      });
    }
  });

  group('ConfirmPage + reto biometrico', () {
    /// Toca el CTA de huella del sheet y deja que el reto falso complete:
    /// un frame + avance para el spinner transitorio y settle para la
    /// salida del sheet + navegacion.
    Future<void> tapSheetAuthorize(WidgetTester tester) async {
      await tester.tap(find.byKey(const Key('biometric-authorize')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      await tester.pumpAndSettle();
    }

    Future<void> pumpPending(
      WidgetTester tester, {
      required _FakeConfirmTransfers transfers,
      required FakePaymentAuthService auth,
      BiometricReader? reader,
    }) async {
      // Superficie amplia y alta: la fuente de tests mide mas ancho que
      // Inter (evita overflows artefacto) y el `ListView`/sheet construyen
      // todo el contenido sin quedar bajo el fold.
      tester.view.physicalSize = const Size(800, 1200);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(() {
        tester.view.resetPhysicalSize();
        tester.view.resetDevicePixelRatio();
      });
      transfers.ownResult = const TransferResult(
        transactionId: 'tx-1',
        type: 'OWN_TRANSFER',
        status: 'PENDING_AUTHORIZATION',
        amountMinor: 25000,
        currency: 'PEN',
        nonce: 'n-1',
      );
      await tester.pumpWidget(
        MaterialApp.router(
          routerConfig: _confirmHarness(
            transfers: transfers,
            authFactory: _depsFactory(auth, reader: reader),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();
    }

    testWidgets('reto exitoso continua al comprobante con clave derivada', (
      tester,
    ) async {
      final transfers = _FakeConfirmTransfers();
      final auth = FakePaymentAuthService();
      await pumpPending(tester, transfers: transfers, auth: auth);

      expect(find.byKey(const Key('biometric-title')), findsOneWidget);

      await tapSheetAuthorize(tester);

      expect(find.text('Transferencia exitosa'), findsOneWidget);
      expect(auth.authorizeCalls, 1);
      // Regresion causa raiz: el `authorize` NUNCA reutiliza la clave del
      // borrador (`k-auth-1`): usa `<borrador>-auth` para no chocar con la
      // UQ global `(key, user_id)` del backend (mismo cuerpo distinto =
      // `409` y reto nunca ejecutado).
      expect(auth.authorizeKeys, ['k-auth-1-auth']);
    });

    testWidgets('biometria fallida muestra el fallback a PIN', (tester) async {
      final transfers = _FakeConfirmTransfers();
      final auth = FakePaymentAuthService();
      await pumpPending(
        tester,
        transfers: transfers,
        auth: auth,
        reader: FakeBiometricReader(available: true, succeeds: false),
      );

      await tapSheetAuthorize(tester);

      expect(find.byKey(const Key('pin-fallback-card')), findsOneWidget);
      expect(find.byKey(const Key('pin-fallback-message')), findsOneWidget);
      expect(auth.authorizeCalls, 0);

      // E2-T31: el PIN se captura DENTRO del flujo (campo en linea), sin
      // navegar a `/login` (la guarda expulsaba al usuario).
      expect(find.byKey(const Key('pin-fallback-field')), findsOneWidget);
      expect(find.byKey(const Key('pin-fallback-submit')), findsOneWidget);
      expect(find.byKey(const Key('pin-fallback-go')), findsNothing);
      expect(find.text('Login con PIN'), findsNothing);
    });

    testWidgets('tap directo en PIN monta el fallback (regresion)', (
      tester,
    ) async {
      final transfers = _FakeConfirmTransfers();
      final auth = FakePaymentAuthService();
      await pumpPending(tester, transfers: transfers, auth: auth);

      expect(find.byKey(const Key('biometric-title')), findsOneWidget);
      // Sin pasar por el controlador: el `onUsePin` antes hacia
      // `pop(usePin)` sin fijar estado y el fallback nunca se montaba.
      await tester.tap(find.byKey(const Key('biometric-pin-fallback')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('pin-fallback-card')), findsOneWidget);
      expect(find.byKey(const Key('pin-fallback-message')), findsOneWidget);
      expect(auth.authorizeCalls, 0);
    });

    testWidgets('409 en authorize reconcilia por GET y va al comprobante', (
      tester,
    ) async {
      final transfers = _FakeConfirmTransfers()
        ..settledDetail = const TransactionDetail(
          transactionId: 'tx-1',
          type: 'OWN_TRANSFER',
          status: 'SETTLED',
          amountMinor: 25000,
          currency: 'PEN',
        );
      final auth = FakePaymentAuthService()
        ..authorizeScript = [
          ApiException(
            code: 'CONFLICT',
            message: 'Idempotency-Key ya usada con un cuerpo distinto.',
          ),
        ];
      await pumpPending(tester, transfers: transfers, auth: auth);

      await tapSheetAuthorize(tester);

      // Exito real tras el `409`: comprobante, sin callejon de duplicado.
      expect(find.text('Transferencia exitosa'), findsOneWidget);
      expect(auth.authorizeKeys, ['k-auth-1-auth']);
    });

    testWidgets('409 con pendiente ofrece PIN tras reintento con clave nueva', (
      tester,
    ) async {
      final transfers = _FakeConfirmTransfers()
        ..settledDetail = const TransactionDetail(
          transactionId: 'tx-1',
          type: 'OWN_TRANSFER',
          status: 'PENDING_AUTHORIZATION',
          amountMinor: 25000,
          currency: 'PEN',
          nonce: 'n-1',
        );
      final auth = FakePaymentAuthService()
        ..authorizeScript = [
          ApiException(code: 'CONFLICT', message: 'En conflicto.'),
          ApiException(code: 'CONFLICT', message: 'En conflicto.'),
        ];
      await pumpPending(tester, transfers: transfers, auth: auth);

      await tapSheetAuthorize(tester);

      expect(find.byKey(const Key('pin-fallback-card')), findsOneWidget);
      // El reintento tras el `409` usa clave NUEVA, no la misma.
      expect(auth.authorizeKeys, [
        'k-auth-1-auth',
        'k-auth-1-auth-retry-1',
      ]);
    });

    testWidgets('EXPIRED_NONCE sin reto nuevo muestra sesion expirada', (
      tester,
    ) async {
      final transfers = _FakeConfirmTransfers();
      final auth = FakePaymentAuthService()
        ..authorizeScript = [
          ApiException(code: 'EXPIRED_NONCE', message: 'Vencido.'),
        ]
        ..refreshResult = const TransactionDetail(
          transactionId: 'tx-1',
          type: 'OWN_TRANSFER',
          status: 'PENDING_AUTHORIZATION',
          amountMinor: 25000,
          currency: 'PEN',
        );
      await pumpPending(tester, transfers: transfers, auth: auth);

      await tapSheetAuthorize(tester);

      expect(find.text('La solicitud expiró'), findsOneWidget);
      // Sin reto nuevo no hay segundo `authorize`: un solo intento con la
      // clave DERIVADA + una rotacion (el reintento con `nonce` nuevo y
      // clave nueva se prueba a nivel controlador).
      expect(auth.authorizeKeys, ['k-auth-1-auth']);
      expect(auth.refreshCalls, 1);
    });

    testWidgets('rechazo por limite diario muestra el error 2', (tester) async {
      final transfers = _FakeConfirmTransfers()
        ..ownResult = const TransferResult(
          transactionId: 'tx-2',
          type: 'OWN_TRANSFER',
          status: 'REJECTED',
          amountMinor: 35000,
          currency: 'PEN',
          feeMinor: 350,
          failureCode: 'DAILY_LIMIT_EXCEEDED',
        );
      final auth = FakePaymentAuthService();
      await tester.pumpWidget(
        MaterialApp.router(
          routerConfig: _confirmHarness(
            transfers: transfers,
            authFactory: _depsFactory(auth),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('confirm-submit')));
      await tester.pumpAndSettle();

      expect(find.text('Superaste tu límite diario'), findsOneWidget);
      expect(find.byKey(const Key('payment-error-primary')), findsOneWidget);
    });
  });
}

/// Extensión solo de tests para variar el `kind` manteniendo el resumen.
extension on PaymentErrorArgs {
  PaymentErrorArgs copyWithKind(PaymentErrorKind kind) => PaymentErrorArgs(
    kind: kind,
    errorCode: errorCode,
    recipientLabel: recipientLabel,
    bankMasked: bankMasked,
    amountLabel: amountLabel,
    feeLabel: feeLabel,
    totalLabel: totalLabel,
    reportRef: reportRef,
    reportDate: reportDate,
  );
}

// ---------------------------------------------------------------------------
// Integración en ConfirmPage.
// ---------------------------------------------------------------------------

const _a1 = Account(
  id: 'a1',
  accountNumberMasked: '•••• 8921',
  type: 'SAVINGS',
  currency: 'PEN',
  availableMinor: 485020,
  heldMinor: 0,
  balanceMinor: 485020,
);

/// Servidor minimo con UQ global `(key, user_id)` para la regresion de la
/// causa raiz: misma clave + cuerpo distinto -> `409`, como el backend.
class _SharedIdempotencyServer {
  final Map<String, String> bodies = {};

  void claim({required String key, required String body}) {
    bodies[key] = body;
  }

  void check({required String key, required String body}) {
    final prior = bodies[key];
    if (prior != null && prior != body) {
      throw ApiException(
        code: 'CONFLICT',
        message: 'Idempotency-Key ya usada con un cuerpo distinto.',
      );
    }
    bodies[key] = body;
  }
}

/// Fake de `authorize` sobre el servidor compartido (semantica real de UQ).
class _SharedAuthFake implements PaymentAuthorizationServiceBase {
  _SharedAuthFake({required this.server});

  final _SharedIdempotencyServer server;

  @override
  Future<AuthorizationResult> authorize({
    required String transactionId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  }) async {
    server.check(
      key: idempotencyKey ?? '',
      body: 'authorize|$transactionId|$nonce|$signature|$deviceId',
    );
    return AuthorizationResult(transactionId: transactionId, status: 'SETTLED');
  }

  @override
  Future<AuthorizationResult> authorizeWithPin({
    required String transactionId,
    required String pin,
    String? idempotencyKey,
  }) async {
    server.check(
      key: idempotencyKey ?? '',
      body: 'authorize-pin|$transactionId|$pin',
    );
    return AuthorizationResult(transactionId: transactionId, status: 'SETTLED');
  }

  @override
  Future<TransactionDetail> refreshChallenge(String transactionId) {
    throw UnimplementedError();
  }
}

/// Fake mínimo del POST de transferencias (el resto del seam no se usa).
class _FakeConfirmTransfers implements TransfersServiceBase {
  TransferResult? ownResult;

  /// Respuesta de `GET /transactions/{id}` para reconciliar `409`.
  /// `null` = no usado (los tests viejos no tocan el `GET`).
  TransactionDetail? settledDetail;
  int getTransactionCalls = 0;

  @override
  Future<TransferResult> createOwnTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) async =>
      ownResult ??
      const TransferResult(
        transactionId: 'tx-1',
        type: 'OWN_TRANSFER',
        status: 'SETTLED',
        amountMinor: 25000,
        currency: 'PEN',
      );

  @override
  Future<List<Account>> accounts() => throw UnimplementedError();

  @override
  Future<List<Beneficiary>> beneficiaries({String? query}) =>
      throw UnimplementedError();

  @override
  Future<TransferResult> createThirdPartyTransfer({
    required String sourceAccountId,
    String? targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    int? feeMinor,
    String? beneficiaryId,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) => throw UnimplementedError();

  @override
  Future<TransferResult> createInterbankTransfer({
    required String sourceAccountId,
    required int amountMinor,
    required String cci,
    String currency = 'PEN',
    String? holderName,
    String? bankCode,
    int? feeMinor,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) => throw UnimplementedError();

  @override
  Future<ResolvedTarget> resolveTarget({
    String? documentType,
    String? documentNumber,
    String? phone,
  }) =>
      throw UnimplementedError();

  @override
  Future<TransactionDetail> getTransaction(String transactionId) async {
    getTransactionCalls++;
    final detail = settledDetail;
    if (detail != null) return detail;
    throw ApiException(code: 'NOT_FOUND', message: 'No encontrada.');
  }

  @override
  Future<ReceiptResult> getReceipt(String transactionId) =>
      throw UnimplementedError();
}

TransferFlowArgs _pendingArgs() {
  final draft = TransferDraft(
    kind: TransferKind.own,
    sourceAccountId: 'a1',
    targetAccountId: 'a2',
    beneficiaryAlias: 'Mamá Lima',
    destinationMasked: '•••• 4521',
    amountMinor: 25000,
    idempotencyKey: 'k-auth-1',
  );
  return TransferFlowArgs(draft: draft, source: _a1);
}

GoRouter _confirmHarness({
  required TransfersServiceBase transfers,
  required PaymentAuthorizationDepsFactory authFactory,
}) {
  return GoRouter(
    initialLocation: '/test-confirm',
    routes: [
      GoRoute(
        path: '/test-confirm',
        builder: (context, state) => ConfirmPage(
          args: _pendingArgs(),
          service: transfers,
          authDepsFactory: authFactory,
        ),
      ),
      GoRoute(
        path: '/transfers/receipt/:id',
        builder: (context, state) =>
            const Scaffold(body: Text('Transferencia exitosa')),
      ),
      GoRoute(
        path: '/login',
        builder: (context, state) =>
            const Scaffold(body: Text('Login con PIN')),
      ),
      GoRoute(
        path: '/home',
        builder: (context, state) => const Scaffold(body: Text('Home')),
      ),
    ],
  );
}

PaymentAuthorizationDepsFactory _depsFactory(
  FakePaymentAuthService service, {
  BiometricReader? reader,
}) {
  return () => _FakeDeps(service: service, reader: reader);
}

/// Deps falsas sin red ni plataforma (firmador determinista en memoria).
class _FakeDeps extends PaymentAuthorizationDeps {
  _FakeDeps({required FakePaymentAuthService service, BiometricReader? reader})
    : _service = service,
      _reader = reader ?? FakeBiometricReader(),
      super(
        service: service,
        reader: reader ?? FakeBiometricReader(),
        session: InMemorySessionRepository(),
        deviceId: 'dev-1',
      );

  final FakePaymentAuthService _service;
  final BiometricReader _reader;

  @override
  PaymentAuthorizationController buildController() {
    return PaymentAuthorizationController(
      service: _service,
      reader: _reader,
      signNonce: (nonce) async => 'sig-of-$nonce',
      deviceId: 'dev-1',
    );
  }
}
