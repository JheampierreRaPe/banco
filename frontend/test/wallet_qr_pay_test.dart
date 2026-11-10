import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/payment_authorization/data/payment_authorization_service.dart';
import 'package:banca_online/features/payment_authorization/payment_authorization_controller.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/wallet/data/qr_payment_service.dart';
import 'package:banca_online/features/wallet/data/wallet_service.dart';
import 'package:banca_online/features/wallet/models/qr_payment.dart';
import 'package:banca_online/features/wallet/models/wallet.dart';
import 'package:banca_online/features/wallet/presentation/qr_pay_page.dart';
import 'package:banca_online/features/wallet/presentation/qr_scan_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

const kQrContent = 'BO-QR1.mock-pagador-contenido-escaneado';

QrPaymentResult _settled() => const QrPaymentResult(
      transactionId: 'txn-1',
      qrPaymentId: 'qrp-1',
      chargeId: 'chg-1',
      merchantId: 'm-1',
      merchantName: 'Café Central',
      amountMinor: 2550,
      currency: 'PEN',
      status: 'SETTLED',
    );

QrPaymentResult _pending() => const QrPaymentResult(
      transactionId: 'txn-2',
      qrPaymentId: 'qrp-2',
      chargeId: 'chg-2',
      merchantId: 'm-1',
      merchantName: 'Café Central',
      amountMinor: 99900,
      currency: 'PEN',
      status: 'PENDING_AUTHORIZATION',
      authMethod: 'BIOMETRIC',
      authNonce: 'nonce-1',
      authExpiresAt: '2026-10-13T01:00:00Z',
    );

/// `SETTLED` del replay tras autorizar el diferido (mismo comercio y
/// monto que el `PENDING`; fix ALTO-1).
QrPaymentResult _settledOverLimit() => const QrPaymentResult(
      transactionId: 'txn-2',
      qrPaymentId: 'qrp-2',
      chargeId: 'chg-2',
      merchantId: 'm-1',
      merchantName: 'Café Central',
      amountMinor: 99900,
      currency: 'PEN',
      status: 'SETTLED',
    );

/// Fake del pago QR (seam `QrPaymentServiceBase`): sin red, con shapes
/// E4-T12 (`POST /payments/qr` -> `data` con `merchant`/`amount_minor`/
/// `status`/`authorization`) + preview (`POST /payments/qr/preview`).
class FakeQrPaymentService implements QrPaymentServiceBase {
  /// Cuerpos enviados a `POST /payments/qr`.
  final List<Map<String, dynamic>> payBodies = [];

  /// `Idempotency-Key` recibidas por intento (reuso en el Reintentar y en
  /// la finalizacion del diferido con la misma clave, ALTO-1).
  final List<String?> payKeys = [];

  /// Respuesta a devolver (por defecto `SETTLED`).
  QrPaymentResult nextResult = _settled();

  /// Cola de respuestas: si no esta vacia, `payQr` consume en orden
  /// (para el camino diferido: `PENDING` -> `SETTLED` tras autorizar).
  final List<QrPaymentResult> payQueue = [];

  /// Si no es `null`, `payQr` lanza este error.
  ApiException? payError;

  /// Si es `true`, el proximo `payQr` falla por red y los siguientes
  /// tienen exito (reuso de clave en el Reintentar).
  bool failNextPay = false;

  /// Vista previa a devolver (por defecto el cargo de `Café Central`).
  QrChargePreview nextPreview = const QrChargePreview(
    chargeId: 'chg-1',
    merchantId: 'm-1',
    merchantName: 'Café Central',
    amountMinor: 2550,
    currency: 'PEN',
    status: 'ACTIVE',
  );

  /// Si no es `null`, `previewQr` lanza este error (422/409/red).
  ApiException? previewError;

  /// Contenidos enviados a `POST /payments/qr/preview`.
  final List<String> previewBodies = [];

  @override
  Future<QrPaymentResult> payQr({
    required String qrContent,
    required String linkId,
    int? amountMinor,
    String? idempotencyKey,
  }) async {
    payBodies.add({
      'qr_content': qrContent,
      'link_id': linkId,
      'amount_minor': ?amountMinor,
    });
    payKeys.add(idempotencyKey);
    if (payError != null) throw payError!;
    if (failNextPay) {
      failNextPay = false;
      throw ApiException.network();
    }
    if (payQueue.isNotEmpty) return payQueue.removeAt(0);
    return nextResult;
  }

  @override
  Future<QrChargePreview> previewQr({required String qrContent}) async {
    previewBodies.add(qrContent);
    if (previewError != null) throw previewError!;
    return nextPreview;
  }
}

/// Fake de billetera solo para elegir fuente (`GET /wallet`).
class FakePayWalletService implements WalletServiceBase {
  FakePayWalletService({this.links = const [], this.hang = false});

  static const defaultLinks = [
    WalletLink(
      id: 'l-1',
      type: 'ACCOUNT',
      accountMasked: '****1234',
      status: 'ACTIVE',
    ),
    WalletLink(
      id: 'l-2',
      type: 'ACCOUNT',
      accountMasked: '****5678',
      status: 'ACTIVE',
    ),
  ];

  final List<WalletLink> links;
  final bool hang;
  ApiException? walletError;

  @override
  Future<Wallet> getWallet() async {
    if (walletError != null) throw walletError!;
    if (hang) await Completer<void>().future;
    return Wallet(walletId: 'w-1', status: 'ACTIVE', links: links);
  }

  @override
  Future<WalletLink> linkAccount(AccountLinkInput input,
          {String? idempotencyKey}) =>
      throw UnimplementedError();

  @override
  Future<WalletLink> linkCard(CardLinkInput input, {String? idempotencyKey}) =>
      throw UnimplementedError();

  @override
  Future<WalletLink> unlink(String id) => throw UnimplementedError();
}

/// Fake de autorizacion (`POST .../authorize`, firma o PIN).
class FakePayAuthService implements PaymentAuthorizationServiceBase {
  int authorizeCalls = 0;
  int authorizePinCalls = 0;
  final List<String?> authorizeKeys = [];
  final List<String> authorizePins = [];
  ApiException? authorizeError;
  ApiException? authorizePinError;

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
    if (authorizeError != null) throw authorizeError!;
    return AuthorizationResult(transactionId: transactionId, status: 'SETTLED');
  }

  @override
  Future<AuthorizationResult> authorizeWithPin({
    required String transactionId,
    required String pin,
    String? idempotencyKey,
  }) async {
    authorizePinCalls++;
    authorizePins.add(pin);
    authorizeKeys.add(idempotencyKey);
    if (authorizePinError != null) throw authorizePinError!;
    return AuthorizationResult(transactionId: transactionId, status: 'SETTLED');
  }

  @override
  Future<TransactionDetail> refreshChallenge(String transactionId) =>
      throw UnimplementedError();
}

PaymentAuthorizationController _bioController(
  FakePayAuthService service, {
  BiometricReader? reader,
}) {
  return PaymentAuthorizationController(
    service: service,
    reader: reader ?? FakeBiometricReader(),
    signNonce: (nonce) async => 'sig-of-$nonce',
    deviceId: 'dev-1',
  );
}

Widget _payHarness({
  FakeQrPaymentService? pay,
  FakePayWalletService? wallet,
  FakePayAuthService? auth,
  PaymentAuthorizationController? controller,
  String qrContent = kQrContent,
  VoidCallback? onRescan,
  VoidCallback? onDone,
}) {
  return MaterialApp(
    home: QrPayPage(
      qrContent: qrContent,
      paymentService: pay ?? FakeQrPaymentService(),
      walletService: wallet ??
          FakePayWalletService(links: FakePayWalletService.defaultLinks),
      authController: controller ??
          (auth == null ? null : _bioController(auth)),
      authService: auth,
      onRescan: onRescan ?? () {},
      onDone: onDone ?? () {},
    ),
  );
}

Future<void> _toSummary(WidgetTester tester) async {
  await tester.pumpAndSettle();
  expect(find.byKey(const Key('qr-pay-summary')), findsOneWidget);
}

void main() {
  group('QrScanPage (escaner)', () {
    QrScannerBuilder fakePreview() {
      return (context, _, _) =>
          const SizedBox(key: Key('qr-pay-fake-scanner'));
    }

    testWidgets('muestra la vista previa real y el campo de inyeccion',
        (tester) async {
      String? scanned;
      await tester.pumpWidget(
        MaterialApp(
          home: QrScanPage(
            onScanned: (v) => scanned = v,
            scannerBuilder: fakePreview(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Pagar con QR'), findsOneWidget);
      // E4-T22: ya no hay placeholder; hay vista previa real (fake aquí).
      expect(
        find.byKey(const Key('qr-pay-camera-preview')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('qr-pay-camera-placeholder')),
        findsNothing,
      );
      expect(
        find.byKey(const Key('qr-pay-camera-denied')),
        findsNothing,
      );
      expect(find.byKey(const Key('qr-pay-fake-scanner')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-scan-input')), findsOneWidget);
      expect(scanned, isNull);
    });

    testWidgets('vacio muestra error accionable sin avanzar', (tester) async {
      var called = false;
      await tester.pumpWidget(
        MaterialApp(
          home: QrScanPage(
            onScanned: (_) => called = true,
            scannerBuilder: fakePreview(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('qr-pay-scan-continue')));
      await tester.pump();

      expect(find.byKey(const Key('qr-pay-scan-error')), findsOneWidget);
      expect(called, isFalse);
    });

    testWidgets('inyecta el qr_content leido (mock del escaner)',
        (tester) async {
      String? scanned;
      await tester.pumpWidget(
        MaterialApp(
          home: QrScanPage(
            onScanned: (v) => scanned = v,
            scannerBuilder: fakePreview(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('qr-pay-scan-input')),
        '  $kQrContent  ',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('qr-pay-scan-continue')));
      await tester.pump();

      expect(scanned, kQrContent);
    });

    testWidgets('deteccion del lector -> _continue una sola vez (E4-T22)',
        (tester) async {
      ValueChanged<String>? captured;
      var calls = 0;
      String? scanned;
      await tester.pumpWidget(
        MaterialApp(
          home: QrScanPage(
            onScanned: (v) {
              calls++;
              scanned = v;
            },
            scannerBuilder: (context, onCode, _) {
              captured = onCode;
              return const SizedBox(key: Key('qr-pay-fake-scanner'));
            },
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qr-pay-camera-preview')), findsOneWidget);
      expect(captured, isNotNull);

      // El lector emite callbacks repetidos: solo la primera navega.
      captured!(kQrContent);
      await tester.pump();
      captured!(kQrContent);
      await tester.pump();

      expect(calls, 1);
      expect(scanned, kQrContent);
    });

    testWidgets('permiso denegado muestra aviso y el campo sigue usable',
        (tester) async {
      String? scanned;
      await tester.pumpWidget(
        MaterialApp(
          home: QrScanPage(
            onScanned: (v) => scanned = v,
            cameraDenied: true,
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('qr-pay-camera-denied')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('qr-pay-camera-preview')),
        findsNothing,
      );
      expect(find.textContaining('Sin acceso a la cámara'), findsOneWidget);
      // Degradacion: el campo manual sigue usable y continua.
      expect(find.byKey(const Key('qr-pay-scan-input')), findsOneWidget);
      await tester.enterText(
        find.byKey(const Key('qr-pay-scan-input')),
        kQrContent,
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('qr-pay-scan-continue')));
      await tester.pump();

      expect(scanned, kQrContent);
    });

    testWidgets('el seam degrada si el lector niega el permiso (E4-T22)',
        (tester) async {
      String? scanned;
      await tester.pumpWidget(
        MaterialApp(
          home: QrScanPage(
            onScanned: (v) => scanned = v,
            scannerBuilder: (context, _, onDenied) => TextButton(
              key: const Key('qr-pay-fake-deny'),
              onPressed: onDenied,
              child: const Text('denegar'),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qr-pay-camera-preview')), findsOneWidget);

      await tester.tap(find.byKey(const Key('qr-pay-fake-deny')));
      await tester.pumpAndSettle();

      expect(
        find.byKey(const Key('qr-pay-camera-denied')),
        findsOneWidget,
      );
      await tester.enterText(
        find.byKey(const Key('qr-pay-scan-input')),
        kQrContent,
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('qr-pay-scan-continue')));
      await tester.pump();

      expect(scanned, kQrContent);
    });
  });

  group('QrPayPage resumen (E4-T13)', () {
    testWidgets('muestra carga mientras lee las fuentes', (tester) async {
      final wallet = FakePayWalletService(
        links: FakePayWalletService.defaultLinks,
        hang: true,
      );
      await tester.pumpWidget(_payHarness(wallet: wallet));
      await tester.pump();

      expect(find.byKey(const Key('qr-pay-loading-links')), findsOneWidget);
    });

    testWidgets('resumen previo sin exponer el qr_content', (tester) async {
      final pay = FakeQrPaymentService();
      await tester.pumpWidget(_payHarness(pay: pay));
      await _toSummary(tester);

      expect(find.text('QR detectado'), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-link-field')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-confirm')), findsOneWidget);
      // Preview del servidor (MEDIO-3): comercio y monto antes de
      // confirmar, valores del backend.
      expect(pay.previewBodies, hasLength(1));
      expect(pay.previewBodies.single, kQrContent);
      expect(find.byKey(const Key('qr-pay-preview-merchant')), findsOneWidget);
      expect(find.text('Café Central'), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-preview-amount')), findsOneWidget);
      expect(find.text('S/ 25.50'), findsOneWidget);
      // El contenido crudo nunca se muestra (E4-T14, sin PII en UI).
      expect(find.textContaining(kQrContent), findsNothing);
      expect(find.textContaining('BO-QR1'), findsNothing);
    });

    testWidgets('sin qr muestra vacio con Re-escanear', (tester) async {
      var rescanned = false;
      await tester.pumpWidget(
        _payHarness(qrContent: '', onRescan: () => rescanned = true),
      );
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qr-pay-error')), findsOneWidget);
      await tester.tap(find.byKey(const Key('qr-pay-rescan')));
      await tester.pump();

      expect(rescanned, isTrue);
    });

    testWidgets('sin fuentes activas ofrece vincular', (tester) async {
      final wallet = FakePayWalletService(links: const []);
      await tester.pumpWidget(_payHarness(wallet: wallet));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qr-pay-empty-links')), findsOneWidget);
      expect(find.text('Vincular fuente'), findsOneWidget);
    });
  });

  group('QrPayPage resumen con preview (MEDIO-3)', () {
    testWidgets('preview 422 (vencido) bloquea el Confirmar sin cobrar',
        (tester) async {
      final pay = FakeQrPaymentService()
        ..previewError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'QR vencido.',
          serverMessage: 'qr expired: cargo vencido hace 5 minutos',
          statusCode: 422,
        );
      await tester.pumpWidget(_payHarness(pay: pay));
      await tester.pumpAndSettle();

      expect(pay.previewBodies, hasLength(1));
      expect(pay.payBodies, isEmpty);
      expect(find.byKey(const Key('qr-pay-error')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-invalid-badge')), findsOneWidget);
      expect(find.textContaining('venció'), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-confirm')), findsNothing);
      expect(find.textContaining(kQrContent), findsNothing);
    });

    testWidgets('preview 409 (ya cobrado) avisa sin segundo cobro',
        (tester) async {
      final pay = FakeQrPaymentService()
        ..previewError = ApiException(
          code: 'CONFLICT',
          message: 'Ya pagado.',
          statusCode: 409,
        );
      await tester.pumpWidget(_payHarness(pay: pay));
      await tester.pumpAndSettle();

      expect(pay.previewBodies, hasLength(1));
      expect(pay.payBodies, isEmpty);
      expect(find.textContaining('ya fue pagado'), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-confirm')), findsNothing);
    });
  });

  group('QrPayPage camino feliz (E4-T13 CA-01/CA-02)', () {
    testWidgets('confirmar en un toque -> recibo con comercio y monto',
        (tester) async {
      final pay = FakeQrPaymentService();
      await tester.pumpWidget(_payHarness(pay: pay));
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();

      // Request: contenido crudo + fuente, con Idempotency-Key.
      expect(pay.payBodies, hasLength(1));
      expect(pay.payBodies.single['qr_content'], kQrContent);
      expect(pay.payBodies.single['link_id'], 'l-1');
      expect(pay.payKeys.single, isNotNull);
      expect(pay.payKeys.single, isNotEmpty);

      // Recibo: valores del servidor, sin recalculo del cliente.
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-result-success')), findsOneWidget);
      expect(find.text('Café Central'), findsOneWidget);
      expect(find.text('S/ 25.50'), findsOneWidget);
    });

    testWidgets('reintento del pago reusa la misma Idempotency-Key',
        (tester) async {
      final pay = FakeQrPaymentService()..failNextPay = true;
      await tester.pumpWidget(_payHarness(pay: pay));
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('qr-pay-error')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-retry')), findsOneWidget);

      await tester.tap(find.byKey(const Key('qr-pay-retry')));
      await tester.pumpAndSettle();

      expect(pay.payBodies, hasLength(2));
      expect(pay.payKeys[0], isNotNull);
      expect(pay.payKeys[1], pay.payKeys[0]);
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
    });
  });

  group('QrPayPage sobre el limite -> biometria/PIN (E4-T13 CA-03)', () {
    testWidgets(
        'PENDING muestra comercio/monto + biometria -> replay misma clave -> SETTLED',
        (tester) async {
      final pay = FakeQrPaymentService()
        ..payQueue.addAll([_pending(), _settledOverLimit()]);
      final auth = FakePayAuthService();
      final reader = FakeBiometricReader();
      await tester.pumpWidget(
        _payHarness(
          pay: pay,
          auth: auth,
          controller: _bioController(auth, reader: reader),
        ),
      );
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();

      // Resumen del servidor ANTES de autorizar (valores del backend).
      expect(find.byKey(const Key('qr-pay-pending-card')), findsOneWidget);
      expect(find.text('Café Central'), findsOneWidget);
      expect(find.text('S/ 999.00'), findsOneWidget);
      expect(
        find.byKey(const Key('qr-pay-authorize-biometric')),
        findsOneWidget,
      );

      await tester.tap(find.byKey(const Key('qr-pay-authorize-biometric')));
      await tester.pumpAndSettle();

      expect(reader.authenticateCalls, 1);
      expect(auth.authorizeCalls, 1);
      // ALTO-1: tras autorizar se reintenta POST /payments/qr con la
      // MISMA Idempotency-Key para finalizar; el recibo solo sale con
      // SETTLED del reintento.
      expect(pay.payBodies, hasLength(2));
      expect(pay.payKeys[0], isNotNull);
      expect(pay.payKeys[1], pay.payKeys[0]);
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
      expect(find.text('S/ 999.00'), findsOneWidget);
    });

    testWidgets('sin biometria cae a PIN y autoriza con replay final',
        (tester) async {
      final pay = FakeQrPaymentService()
        ..payQueue.addAll([_pending(), _settledOverLimit()]);
      final auth = FakePayAuthService();
      await tester.pumpWidget(
        _payHarness(
          pay: pay,
          auth: auth,
          controller: _bioController(
            auth,
            reader: FakeBiometricReader(
              available: false,
              succeeds: false,
            ),
          ),
        ),
      );
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('qr-pay-authorize-biometric')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qr-pay-auth-message')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-pin-field')), findsOneWidget);
      expect(auth.authorizeCalls, isZero);

      await tester.enterText(
        find.byKey(const Key('qr-pay-pin-field')),
        '1234',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('qr-pay-authorize-pin')));
      await tester.pumpAndSettle();

      expect(auth.authorizePinCalls, 1);
      expect(auth.authorizePins.single, '1234');
      // ALTO-1: el PIN tambien finaliza con el replay de la misma clave.
      expect(pay.payBodies, hasLength(2));
      expect(pay.payKeys[1], pay.payKeys[0]);
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
    });

    testWidgets('PIN con formato invalido no llama al servidor',
        (tester) async {
      final pay = FakeQrPaymentService()..nextResult = _pending();
      final auth = FakePayAuthService();
      await tester.pumpWidget(
        _payHarness(
          pay: pay,
          auth: auth,
          controller: _bioController(
            auth,
            reader: FakeBiometricReader(
              available: false,
              succeeds: false,
            ),
          ),
        ),
      );
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('qr-pay-authorize-biometric')));
      await tester.pumpAndSettle();

      await tester.enterText(find.byKey(const Key('qr-pay-pin-field')), '12');
      await tester.pump();
      await tester.tap(find.byKey(const Key('qr-pay-authorize-pin')));
      await tester.pump();

      expect(
        find.text('Ingresa tu PIN de seguridad (4 a 6 dígitos).'),
        findsOneWidget,
      );
      expect(auth.authorizePinCalls, isZero);
    });

    testWidgets('PIN incorrecto muestra error sin romper el flujo',
        (tester) async {
      final pay = FakeQrPaymentService()..nextResult = _pending();
      final auth = FakePayAuthService()
        ..authorizePinError = ApiException(
          code: 'NOT_AUTHORIZED',
          message: 'No tienes permiso para esta operacion.',
          statusCode: 403,
        );
      await tester.pumpWidget(
        _payHarness(
          pay: pay,
          auth: auth,
          controller: _bioController(
            auth,
            reader: FakeBiometricReader(
              available: false,
              succeeds: false,
            ),
          ),
        ),
      );
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('qr-pay-authorize-biometric')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('qr-pay-pin-field')),
        '0000',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('qr-pay-authorize-pin')));
      await tester.pumpAndSettle();

      expect(find.text('PIN incorrecto. Inténtalo de nuevo.'), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-pending-card')), findsOneWidget);
    });
  });

  group('QrPayPage errores QR (E4-T14)', () {
    Future<void> payWithError(
      WidgetTester tester,
      ApiException error, {
      FakeQrPaymentService? pay,
      VoidCallback? onRescan,
    }) async {
      final service = pay ?? FakeQrPaymentService()..payError = error;
      await tester.pumpWidget(_payHarness(pay: service, onRescan: onRescan));
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();
    }

    testWidgets('QR vencido: alerta + mensaje + Re-escanear', (tester) async {
      var rescanned = false;
      final pay = FakeQrPaymentService()
        ..payError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'QR vencido.',
          serverMessage: 'qr expired: cargo vencido hace 5 minutos',
          statusCode: 422,
        );
      await payWithError(tester, pay.payError!, pay: pay,
          onRescan: () => rescanned = true);

      expect(find.byKey(const Key('qr-pay-error')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-invalid-badge')), findsOneWidget);
      expect(find.text('QR inválido'), findsOneWidget);
      expect(find.textContaining('venció'), findsOneWidget);
      expect(find.textContaining('No se descontó nada'), findsOneWidget);
      expect(find.textContaining(kQrContent), findsNothing);

      await tester.tap(find.byKey(const Key('qr-pay-rescan')));
      await tester.pump();
      expect(rescanned, isTrue);
    });

    testWidgets('QR manipulado: alerta de seguridad', (tester) async {
      await payWithError(
        tester,
        ApiException(
          code: 'VALIDATION_ERROR',
          message: 'Firma invalida.',
          serverMessage: 'signature mismatch: firma HMAC no coincide',
          statusCode: 422,
        ),
      );

      expect(find.byKey(const Key('qr-pay-invalid-badge')), findsOneWidget);
      expect(find.textContaining('manipulado'), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-rescan')), findsOneWidget);
    });

    testWidgets('QR corrupto generico: alerta + Re-escanear', (tester) async {
      await payWithError(
        tester,
        ApiException(
          code: 'VALIDATION_ERROR',
          message: 'Invalido.',
          serverMessage: 'payload ilegible',
          statusCode: 422,
        ),
      );

      expect(find.byKey(const Key('qr-pay-invalid-badge')), findsOneWidget);
      expect(find.textContaining('corrupto o no se puede leer'), findsOneWidget);
    });

    testWidgets('QR ajeno: mensaje de comercio no reconocido', (tester) async {
      await payWithError(
        tester,
        ApiException(
          code: 'VALIDATION_ERROR',
          message: 'Invalido.',
          serverMessage: 'cargo ajeno: emisor desconocido',
          statusCode: 422,
        ),
      );

      expect(find.byKey(const Key('qr-pay-invalid-badge')), findsOneWidget);
      expect(find.textContaining('No reconocemos este QR'), findsOneWidget);
    });

    testWidgets('QR ya pagado (409): sin segundo cobro', (tester) async {
      final pay = FakeQrPaymentService()
        ..payError = ApiException(
          code: 'CONFLICT',
          message: 'Conflicto.',
          statusCode: 409,
        );
      await payWithError(tester, pay.payError!, pay: pay);

      expect(find.textContaining('ya fue pagado'), findsOneWidget);
      expect(find.textContaining('no se realizó un segundo cobro'),
          findsOneWidget);
      expect(pay.payBodies, hasLength(1));
      expect(find.byKey(const Key('qr-pay-rescan')), findsOneWidget);
    });

    testWidgets('red: Reintentar y recupera', (tester) async {
      final pay = FakeQrPaymentService()..failNextPay = true;
      await tester.pumpWidget(_payHarness(pay: pay));
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();

      expect(find.textContaining('Sin conexión'), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-retry')), findsOneWidget);

      await tester.tap(find.byKey(const Key('qr-pay-retry')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
    });

    testWidgets('REJECTED sin fondos: mensaje sin descontar', (tester) async {
      final pay = FakeQrPaymentService()
        ..nextResult = const QrPaymentResult(
          transactionId: 'txn-3',
          qrPaymentId: 'qrp-3',
          chargeId: 'chg-3',
          merchantId: 'm-1',
          merchantName: 'Café Central',
          amountMinor: 50000,
          currency: 'PEN',
          status: 'REJECTED',
          failureCode: 'INSUFFICIENT_FUNDS',
        );
      await tester.pumpWidget(_payHarness(pay: pay));
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qr-pay-error')), findsOneWidget);
      expect(find.textContaining('saldo suficiente'), findsOneWidget);
      expect(find.textContaining('no se debitó nada'), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-result-success')), findsNothing);
    });

    testWidgets(
        'REJECTED INSUFFICIENT_FUNDS -> Reintentar usa NUEVA '
        'Idempotency-Key y liquida (fix N-1)', (tester) async {
      // Primer intento: REJECTED definitivo (resultado conocido, sin
      // mover dinero). Segundo intento (tras recargar): SETTLED.
      final rejected = const QrPaymentResult(
        transactionId: 'txn-3',
        qrPaymentId: 'qrp-3',
        chargeId: 'chg-3',
        merchantId: 'm-1',
        merchantName: 'Café Central',
        amountMinor: 50000,
        currency: 'PEN',
        status: 'REJECTED',
        failureCode: 'INSUFFICIENT_FUNDS',
      );
      final pay = FakeQrPaymentService()
        ..payQueue.addAll([rejected, _settled()]);
      await tester.pumpWidget(_payHarness(pay: pay));
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qr-pay-error')), findsOneWidget);
      expect(find.textContaining('saldo suficiente'), findsOneWidget);
      // El rechazo definitivo ofrece Reintentar (misma pantalla).
      expect(find.byKey(const Key('qr-pay-retry')), findsOneWidget);
      expect(pay.payKeys.single, isNotNull);

      await tester.tap(find.byKey(const Key('qr-pay-retry')));
      await tester.pumpAndSettle();

      // Nueva intención: la segunda llamada usa una clave DISTINTA y
      // el recibo confirma el cobro (sin replay del REJECTED cacheado).
      expect(pay.payBodies, hasLength(2));
      expect(pay.payKeys[0], isNotNull);
      expect(pay.payKeys[1], isNotNull);
      expect(pay.payKeys[1], isNot(equals(pay.payKeys[0])));
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-result-success')), findsOneWidget);
    });

    testWidgets(
        'fallo definitivo por HTTP INSUFFICIENT_FUNDS -> Reintentar '
        'rota la clave; red conserva la clave (fix N-1)',
        (tester) async {
      // Camino HTTP definitivo: primer payQr lanza INSUFFICIENT_FUNDS,
      // el Reintentar debe usar clave nueva y liquidar.
      final pay = FakeQrPaymentService()
        ..payError = ApiException(
          code: 'INSUFFICIENT_FUNDS',
          message: 'Sin fondos.',
          statusCode: 402,
        );
      await tester.pumpWidget(_payHarness(pay: pay));
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('qr-pay-error')), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-retry')), findsOneWidget);
      final firstKey = pay.payKeys.single;

      // El usuario recarga y reintenta: el servicio ahora liquida.
      pay.payError = null;
      pay.nextResult = _settled();
      await tester.tap(find.byKey(const Key('qr-pay-retry')));
      await tester.pumpAndSettle();

      expect(pay.payBodies, hasLength(2));
      expect(pay.payKeys[1], isNotNull);
      expect(pay.payKeys[1], isNot(equals(firstKey)));
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
    });

    testWidgets('REJECTED limite diario: mensaje accionable', (tester) async {
      final pay = FakeQrPaymentService()
        ..nextResult = const QrPaymentResult(
          transactionId: 'txn-4',
          qrPaymentId: 'qrp-4',
          chargeId: 'chg-4',
          merchantId: 'm-1',
          merchantName: 'Café Central',
          amountMinor: 50000,
          currency: 'PEN',
          status: 'REJECTED',
          failureCode: 'DAILY_LIMIT_EXCEEDED',
        );
      await tester.pumpWidget(_payHarness(pay: pay));
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();

      expect(find.textContaining('límite diario'), findsOneWidget);
      expect(find.byKey(const Key('qr-pay-result-success')), findsNothing);
    });

    testWidgets('desde el error se puede re-escanear y reintentar',
        (tester) async {
      var rescanned = false;
      final pay = FakeQrPaymentService()..failNextPay = true;
      await tester.pumpWidget(
        _payHarness(pay: pay, onRescan: () => rescanned = true),
      );
      await _toSummary(tester);

      await tester.tap(find.byKey(const Key('qr-pay-confirm')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('qr-pay-error')), findsOneWidget);

      // El flujo no se rompe: hay salida al escaner y reintento.
      expect(find.byKey(const Key('qr-pay-retry')), findsOneWidget);
      await tester.tap(find.byKey(const Key('qr-pay-retry')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('qr-pay-receipt')), findsOneWidget);
      expect(rescanned, isFalse);
    });
  });

  group('qrPayFailureFor (mapeo E4-T14)', () {
    test('mapea cada codigo a mensaje y recuperacion', () {
      expect(
        qrPayFailureFor(code: 'VALIDATION_ERROR', serverHint: 'expirado')
            .invalidQr,
        isTrue,
      );
      expect(
        qrPayFailureFor(code: 'CONFLICT').recovery,
        QrPayRecovery.rescan,
      );
      expect(
        qrPayFailureFor(code: 'NETWORK_ERROR').recovery,
        QrPayRecovery.retry,
      );
      expect(
        qrPayFailureFor(code: 'UNKNOWN', failureCode: 'INSUFFICIENT_FUNDS')
            .message,
        contains('saldo suficiente'),
      );
      expect(
        qrPayFailureFor(code: 'UNKNOWN', failureCode: 'DAILY_LIMIT_EXCEEDED')
            .message,
        contains('límite diario'),
      );
      // Sin PII: ningun mensaje incluye contenidos escaneados.
      for (final f in [
        qrPayFailureFor(code: 'VALIDATION_ERROR'),
        qrPayFailureFor(code: 'CONFLICT'),
        qrPayFailureFor(code: 'NETWORK_ERROR'),
      ]) {
        expect(f.message, isNot(contains('BO-QR1')));
      }
    });

    test('formatea montos del servidor sin recalculo', () {
      expect(formatQrMinor(2550, 'PEN'), 'S/ 25.50');
      expect(formatQrMinor(99900, 'PEN'), 'S/ 999.00');
      expect(formatQrMinor(100, 'USD'), 'US\$ 1.00');
    });
  });

  group('QrPaymentResult (modelo E4-T13)', () {
    test('parsea data con merchant y authorization tal cual', () {
      final result = QrPaymentResult.fromJson(
        {
          'transaction_id': 'txn-2',
          'qr_payment_id': 'qrp-2',
          'charge_id': 'chg-2',
          'merchant': {'merchant_id': 'm-1', 'business_name': 'Café Central'},
          'amount_minor': 99900,
          'currency': 'PEN',
          'status': 'PENDING_AUTHORIZATION',
          'authorization': {
            'method': 'BIOMETRIC',
            'nonce': 'nonce-1',
            'expires_at': '2026-10-13T01:00:00Z',
          },
        },
        replayed: true,
      );

      expect(result.transactionId, 'txn-2');
      expect(result.merchantName, 'Café Central');
      expect(result.amountMinor, 99900);
      expect(result.isPendingAuth, isTrue);
      expect(result.authNonce, 'nonce-1');
      expect(result.replayed, isTrue);
      expect(result.statusLabel, 'Por autorizar');
    });
  });

  group('QrPaymentService (HTTP)', () {
    ApiClient stubClient(
      Response<dynamic> Function(RequestOptions options) handler, {
      Map<String, dynamic>? Function(RequestOptions options)? onRequest,
    }) {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, h) {
            onRequest?.call(options);
            h.resolve(handler(options));
          },
        ),
      );
      return api;
    }

    const payBody = {
      'data': {
        'transaction_id': 'txn-1',
        'qr_payment_id': 'qrp-1',
        'charge_id': 'chg-1',
        'merchant': {'merchant_id': 'm-1', 'business_name': 'Café Central'},
        'amount_minor': 2550,
        'currency': 'PEN',
        'status': 'SETTLED',
      },
      'meta': {'request_id': 'r-1', 'replayed': false},
    };

    test('payQr envia qr_content + link_id con Idempotency-Key', () async {
      Map<String, dynamic>? capturedBody;
      String? path;
      String? idempotencyKey;
      final api = stubClient(
        (o) => Response(requestOptions: o, statusCode: 201, data: payBody),
        onRequest: (o) {
          path = o.path;
          idempotencyKey = o.headers['Idempotency-Key']?.toString();
          if (o.data is Map) {
            capturedBody = Map<String, dynamic>.from(o.data as Map);
          }
          return null;
        },
      );

      final result = await QrPaymentService(api: api).payQr(
        qrContent: kQrContent,
        linkId: 'l-1',
        idempotencyKey: 'key-1',
      );

      expect(path, endsWith('/payments/qr'));
      expect(capturedBody!['qr_content'], kQrContent);
      expect(capturedBody!['link_id'], 'l-1');
      expect(idempotencyKey, 'key-1');
      expect(result.merchantName, 'Café Central');
      expect(result.amountMinor, 2550);
      expect(result.isSettled, isTrue);
    });

    test('422 se propaga como VALIDATION_ERROR', () async {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) => handler.reject(
            DioException(
              requestOptions: options,
              response: Response(
                requestOptions: options,
                statusCode: 422,
                data: {
                  'error': {
                    'code': 'VALIDATION_ERROR',
                    'message': 'QR vencido.',
                  },
                },
              ),
              type: DioExceptionType.badResponse,
            ),
          ),
        ),
      );

      try {
        await QrPaymentService(api: api).payQr(
          qrContent: kQrContent,
          linkId: 'l-1',
          idempotencyKey: 'key-1',
        );
        fail('debió lanzar ApiException');
      } on ApiException catch (e) {
        expect(e.code, 'VALIDATION_ERROR');
      }
    });

    test('409 se propaga como CONFLICT (ya pagado)', () async {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) => handler.reject(
            DioException(
              requestOptions: options,
              response: Response(
                requestOptions: options,
                statusCode: 409,
                data: {
                  'error': {'code': 'CONFLICT', 'message': 'Ya pagado.'},
                },
              ),
              type: DioExceptionType.badResponse,
            ),
          ),
        ),
      );

      try {
        await QrPaymentService(api: api).payQr(
          qrContent: kQrContent,
          linkId: 'l-1',
          idempotencyKey: 'key-1',
        );
        fail('debió lanzar ApiException');
      } on ApiException catch (e) {
        expect(e.code, 'CONFLICT');
      }
    });

    const previewBody = {
      'data': {
        'charge_id': 'chg-1',
        'merchant': {'merchant_id': 'm-1', 'business_name': 'Café Central'},
        'amount_minor': 2550,
        'currency': 'PEN',
        'expires_at': '2026-10-13T02:00:00Z',
        'status': 'ACTIVE',
      },
      'meta': {'request_id': 'r-2'},
    };

    test('previewQr envia qr_content a /payments/qr/preview', () async {
      Map<String, dynamic>? capturedBody;
      String? path;
      final api = stubClient(
        (o) => Response(requestOptions: o, statusCode: 200, data: previewBody),
        onRequest: (o) {
          path = o.path;
          if (o.data is Map) {
            capturedBody = Map<String, dynamic>.from(o.data as Map);
          }
          return null;
        },
      );

      final preview = await QrPaymentService(api: api).previewQr(
        qrContent: kQrContent,
      );

      expect(path, endsWith('/payments/qr/preview'));
      expect(capturedBody!['qr_content'], kQrContent);
      expect(preview.merchantName, 'Café Central');
      expect(preview.amountMinor, 2550);
      expect(preview.chargeId, 'chg-1');
    });

    test('preview 422/409 se propagan sin cobrar', () async {
      for (final status in [422, 409]) {
        final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
        final api = ApiClient.create(
          session: InMemorySessionRepository(initialAccessToken: 'token'),
          dioOverride: dio,
        );
        dio.interceptors.add(
          InterceptorsWrapper(
            onRequest: (options, handler) => handler.reject(
              DioException(
                requestOptions: options,
                response: Response(
                  requestOptions: options,
                  statusCode: status,
                  data: {
                    'error': {
                      'code': status == 422 ? 'VALIDATION_ERROR' : 'CONFLICT',
                      'message': status == 422 ? 'QR vencido.' : 'Ya pagado.',
                    },
                  },
                ),
                type: DioExceptionType.badResponse,
              ),
            ),
          ),
        );
        try {
          await QrPaymentService(api: api).previewQr(qrContent: kQrContent);
          fail('debió lanzar ApiException ($status)');
        } on ApiException catch (e) {
          expect(
            e.code,
            status == 422 ? 'VALIDATION_ERROR' : 'CONFLICT',
          );
        }
      }
    });
  });
}
