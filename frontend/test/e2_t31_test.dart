// Pruebas E2-T31 (HU08): autorizar el pago con PIN dentro del flujo.
//
// - El servicio envia `{pin}` (sin `signature`) al `authorize`.
// - El fallback tras el reto muestra un campo de PIN EN EL FLUJO (sin
//   navegar a `/login`): PIN correcto -> comprobante; PIN incorrecto ->
//   mensaje claro y reintento; bloqueo preventivo -> estado terminal.
// - La via por firma queda intacta (regresion E2-T21 en su propio archivo).
library;

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/payment_authorization/data/payment_authorization_service.dart';
import 'package:banca_online/features/payment_authorization/payment_authorization_routes.dart';
import 'package:banca_online/features/transfers/data/transfers_service.dart';
import 'package:banca_online/features/transfers/models/resolve_target.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/transfers/presentation/confirm_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

/// Fake del servicio de autorizacion con guion para la via PIN (E2-T31).
///
/// Modela la idempotencia real del backend (UQ global `(key, user_id)`):
/// misma clave + PIN distinto => `409 CONFLICT` (el `pin_digest` cambia).
/// Sin este modelado, reutilizar la clave tras un PIN incorrecto pareceria
/// funcionar y el defecto D2 (tercer tap) no se detectaria.
class FakePinAuthService implements PaymentAuthorizationServiceBase {
  List<Object> authorizePinScript = const [];
  int authorizePinCalls = 0;
  List<String> authorizePins = [];
  List<String?> authorizePinKeys = [];
  final Map<String?, String> _pinByKey = {};

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
    throw UnimplementedError('E2-T31 usa la via PIN');
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
    // Idempotencia real: el fallo previo queda snapshotteado bajo la clave
    // (digest del PIN). Misma clave + PIN distinto = `409`.
    final seen = _pinByKey[idempotencyKey];
    if (seen != null && seen != pin) {
      throw ApiException(
        code: 'CONFLICT',
        message: 'Esta operación ya fue procesada. Revisa tus movimientos '
            'antes de reintentarla.',
      );
    }
    _pinByKey[idempotencyKey] = pin;
    final item = i < authorizePinScript.length
        ? authorizePinScript[i]
        : authorizePinScript.isEmpty
        ? _success()
        : authorizePinScript.last;
    if (item is ApiException) throw item;
    return item as AuthorizationResult;
  }

  @override
  Future<TransactionDetail> refreshChallenge(String transactionId) =>
      throw UnimplementedError();
}

/// Fake mínimo del POST de transferencias (borrador pendiente de reto).
class FakePinTransfers implements TransfersServiceBase {
  @override
  Future<TransferResult> createOwnTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) async => const TransferResult(
    transactionId: 'tx-1',
    type: 'OWN_TRANSFER',
    status: 'PENDING_AUTHORIZATION',
    amountMinor: 25000,
    currency: 'PEN',
    nonce: 'n-1',
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
  }) => throw UnimplementedError();

  @override
  Future<TransactionDetail> getTransaction(String transactionId) async =>
      throw ApiException(code: 'NOT_FOUND', message: 'No encontrada.');

  @override
  Future<ReceiptResult> getReceipt(String transactionId) =>
      throw UnimplementedError();
}

const _a1 = Account(
  id: 'a1',
  accountNumberMasked: '•••• 8921',
  type: 'SAVINGS',
  currency: 'PEN',
  availableMinor: 485020,
  heldMinor: 0,
  balanceMinor: 485020,
);

TransferFlowArgs _pendingArgs() {
  final draft = TransferDraft(
    kind: TransferKind.own,
    sourceAccountId: 'a1',
    targetAccountId: 'a2',
    beneficiaryAlias: 'Mamá Lima',
    destinationMasked: '•••• 4521',
    amountMinor: 25000,
    idempotencyKey: 'k-pin-1',
  );
  return TransferFlowArgs(draft: draft, source: _a1);
}

class _FakePinDeps extends PaymentAuthorizationDeps {
  _FakePinDeps({required FakePinAuthService service, BiometricReader? reader})
    : super(
        service: service,
        reader: reader ?? FakeBiometricReader(),
        session: InMemorySessionRepository(),
        deviceId: 'dev-1',
      );
}

GoRouter _pinHarness({
  required FakePinTransfers transfers,
  required FakePinAuthService auth,
  BiometricReader? reader,
}) {
  PaymentAuthorizationDepsFactory factory() =>
      () => _FakePinDeps(service: auth, reader: reader);
  return GoRouter(
    initialLocation: '/test-confirm',
    routes: [
      GoRoute(
        path: '/test-confirm',
        builder: (context, state) => ConfirmPage(
          args: _pendingArgs(),
          service: transfers,
          authDepsFactory: factory(),
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

/// Lleva la confirmacion hasta el fallback a PIN (biometria fallida).
Future<void> _pumpToPinFallback(
  WidgetTester tester, {
  required FakePinTransfers transfers,
  required FakePinAuthService auth,
}) async {
  tester.view.physicalSize = const Size(800, 1200);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
  await tester.pumpWidget(
    MaterialApp.router(
      routerConfig: _pinHarness(
        transfers: transfers,
        auth: auth,
        reader: FakeBiometricReader(available: true, succeeds: false),
      ),
    ),
  );
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const Key('confirm-submit')));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const Key('biometric-authorize')));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
  await tester.pumpAndSettle();
  expect(find.byKey(const Key('pin-fallback-card')), findsOneWidget);
}

Future<void> _submitPin(WidgetTester tester, String pin) async {
  await tester.enterText(find.byKey(const Key('pin-fallback-field')), pin);
  await tester.pump();
  await tester.tap(find.byKey(const Key('pin-fallback-submit')));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 100));
  await tester.pumpAndSettle();
}

void main() {
  group('E2-T31 servicio authorizeWithPin', () {
    test('envia {pin} sin firma y con Idempotency-Key', () async {
      final dio = Dio();
      String? capturedPath;
      Object? capturedData;
      String? capturedKey;
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            capturedPath = options.path;
            capturedData = options.data;
            capturedKey = options.headers['Idempotency-Key']?.toString();
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {
                    'transaction_id': 'tx-1',
                    'status': 'SETTLED',
                    'tracking': <String, dynamic>{},
                    'failure_code': null,
                  },
                  'meta': {'request_id': 'r-1', 'replayed': false},
                },
              ),
            );
          },
        ),
      );
      final service = PaymentAuthorizationService(
        api: ApiClient(dio: dio, uuid: const Uuid()),
      );

      final result = await service.authorizeWithPin(
        transactionId: 'tx-1',
        pin: '4829',
        idempotencyKey: 'k-pin-1',
      );

      expect(result.status, 'SETTLED');
      expect(capturedPath, '/transactions/tx-1/authorize');
      final body = capturedData as Map;
      expect(body['pin'], '4829');
      expect(body.containsKey('signature'), isFalse);
      expect(body.containsKey('device_id'), isFalse);
      expect(capturedKey, 'k-pin-1');
    });
  });

  group('E2-T31 fallback a PIN en el flujo', () {
    testWidgets('campo PIN en el flujo: exito va al comprobante', (
      tester,
    ) async {
      final transfers = FakePinTransfers();
      final auth = FakePinAuthService();
      await _pumpToPinFallback(tester, transfers: transfers, auth: auth);

      // El PIN se captura en linea: campo + accion, sin ir a `/login`.
      expect(find.byKey(const Key('pin-fallback-field')), findsOneWidget);
      expect(find.byKey(const Key('pin-fallback-submit')), findsOneWidget);

      await _submitPin(tester, '4829');

      expect(auth.authorizePinCalls, 1);
      expect(auth.authorizePins, ['4829']);
      // Misma derivacion que la firma (`<borrador>-auth`, UQ global).
      expect(auth.authorizePinKeys, ['k-pin-1-auth']);
      expect(find.text('Transferencia exitosa'), findsOneWidget);
      expect(find.text('Login con PIN'), findsNothing);
    });

    testWidgets('PIN incorrecto muestra mensaje y permite reintentar', (
      tester,
    ) async {
      final transfers = FakePinTransfers();
      final auth = FakePinAuthService()
        ..authorizePinScript = [
          ApiException(
            code: 'NOT_AUTHORIZED',
            message: 'No pudimos verificar tu identidad.',
          ),
          const AuthorizationResult(
            transactionId: 'tx-1',
            status: 'SETTLED',
          ),
        ];
      await _pumpToPinFallback(tester, transfers: transfers, auth: auth);

      await _submitPin(tester, '0000');

      expect(auth.authorizePinCalls, 1);
      expect(auth.authorizePins, ['0000']);
      // Sigue en el flujo: mensaje claro, sin navegar a `/login`.
      expect(find.byKey(const Key('pin-fallback-error')), findsOneWidget);
      expect(find.byKey(const Key('pin-fallback-card')), findsOneWidget);
      expect(find.text('Login con PIN'), findsNothing);

      // Reintento con el PIN correcto continua al comprobante EN UN SOLO
      // PASO (regresion D2: sin rotar la clave, el fake de idempotencia
      // devuelve `409` y haria falta un tercer tap).
      await _submitPin(tester, '4829');

      expect(auth.authorizePinCalls, 2);
      expect(auth.authorizePins, ['0000', '4829']);
      // La clave rota tras el fallo (mismo patron que la firma).
      expect(auth.authorizePinKeys.length, 2);
      expect(auth.authorizePinKeys[0], 'k-pin-1-auth');
      expect(auth.authorizePinKeys[1], isNot(auth.authorizePinKeys[0]));
      expect(auth.authorizePinKeys[1], 'k-pin-1-auth-retry-1');
      expect(find.text('Transferencia exitosa'), findsOneWidget);
      expect(find.byKey(const Key('pin-fallback-error')), findsNothing);
    });

    testWidgets('bloqueo preventivo muestra el estado terminal', (
      tester,
    ) async {
      final transfers = FakePinTransfers();
      final auth = FakePinAuthService()
        ..authorizePinScript = [
          const AuthorizationResult(
            transactionId: 'tx-1',
            status: 'REJECTED',
            failureCode: 'BIOMETRIC_PREVENTIVE_BLOCK',
          ),
        ];
      await _pumpToPinFallback(tester, transfers: transfers, auth: auth);

      await _submitPin(tester, '0000');

      expect(auth.authorizePinCalls, 1);
      expect(find.byKey(const Key('pin-fallback-card')), findsNothing);
      expect(find.text('Acceso protegido por seguridad'), findsOneWidget);
      expect(find.text('Login con PIN'), findsNothing);
    });

    testWidgets('no navega a /login al pedir el PIN', (tester) async {
      final transfers = FakePinTransfers();
      final auth = FakePinAuthService();
      await _pumpToPinFallback(tester, transfers: transfers, auth: auth);

      expect(find.text('Login con PIN'), findsNothing);
      // Sin CTA que expulse: la llave vieja no existe.
      expect(find.byKey(const Key('pin-fallback-go')), findsNothing);
    });
  });
}
