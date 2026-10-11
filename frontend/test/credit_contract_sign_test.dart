import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/credits/data/credit_contract_service.dart';
import 'package:banca_online/features/credits/data/credit_sign_controller.dart';
import 'package:banca_online/features/credits/models/credit_contract.dart';
import 'package:banca_online/features/credits/models/loan_application.dart';
import 'package:banca_online/features/credits/presentation/credit_contract_page.dart';
import 'package:banca_online/features/credits/presentation/credit_sign_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fake contractual (seam `CreditContractServiceBase`): sin red, con shapes
/// `docs/05` §6.4 (`{data, meta}` ya parseados a modelos). El dictamen y los
/// documentos los decide el mock, no la UI (cliente delgado, E3-T15).
class FakeCreditContractService implements CreditContractServiceBase {
  ContractBundle bundle = _bundle();
  ApiException? bundleError;

  SignChallenge challenge = const SignChallenge(
    applicationId: 'app-1',
    nonce: 'nonce-1',
    expiresIn: 120,
    status: 'GENERATED',
  );
  ApiException? challengeError;

  /// Respuestas encoladas por llamada de firma biometrica (cada item:
  /// [ContractSignatureResult] o [ApiException]).
  final List<Object> bioScript = [];

  /// Respuestas encoladas por llamada de firma con PIN.
  final List<Object> pinScript = [];

  ContractSignatureResult signedResult = const ContractSignatureResult(
    applicationId: 'app-1',
    contractId: 'c-1',
    status: 'SIGNED',
    signedAt: '2026-10-11T12:00:00Z',
    via: 'device',
  );

  int getCalls = 0;
  int challengeCalls = 0;
  int bioCalls = 0;
  int pinCalls = 0;
  final List<String?> bioKeys = [];
  final List<String?> pinKeys = [];
  final List<Map<String, String>> bioBodies = [];
  final List<String> downloadCalls = [];
  ApiException? downloadError;

  static ContractBundle _bundle() {
    return ContractBundle.fromJson({
      'application_id': 'app-1',
      'status': 'GENERATED',
      'documents': [
        {
          'contract_id': 'c-1',
          'doc_type': 'CONTRATO',
          'doc': 'contrato',
          'doc_ref': 'ref-1',
          'doc_hash': 'hash-1',
          'status': 'GENERATED',
        },
        {
          'contract_id': 'c-2',
          'doc_type': 'PAGARE',
          'doc': 'pagare',
          'doc_ref': 'ref-2',
          'doc_hash': 'hash-2',
          'status': 'GENERATED',
        },
        {
          'contract_id': 'c-3',
          'doc_type': 'RESUMEN',
          'doc': 'resumen',
          'doc_ref': 'ref-3',
          'doc_hash': 'hash-3',
          'status': 'GENERATED',
        },
      ],
    });
  }

  @override
  Future<ContractBundle> getContract(String applicationId) async {
    getCalls++;
    final error = bundleError;
    if (error != null) throw error;
    return bundle;
  }

  @override
  Future<ContractPdf> downloadDocument(
    String applicationId,
    String doc,
  ) async {
    downloadCalls.add(doc);
    final error = downloadError;
    if (error != null) throw error;
    return ContractPdf(bytes: const [1, 2, 3], suggestedFilename: '$doc.pdf');
  }

  @override
  Future<SignChallenge> requestChallenge(
    String applicationId, {
    String? deviceId,
  }) async {
    challengeCalls++;
    final error = challengeError;
    if (error != null) throw error;
    return challenge;
  }

  @override
  Future<ContractSignatureResult> signWithBiometrics({
    required String applicationId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  }) async {
    bioCalls++;
    bioKeys.add(idempotencyKey);
    bioBodies.add({'nonce': nonce, 'signature': signature});
    if (bioScript.isNotEmpty) {
      final next = bioScript.removeAt(0);
      if (next is ApiException) throw next;
      return next as ContractSignatureResult;
    }
    return signedResult;
  }

  @override
  Future<ContractSignatureResult> signWithPin({
    required String applicationId,
    required String pin,
    String? idempotencyKey,
  }) async {
    pinCalls++;
    pinKeys.add(idempotencyKey);
    if (pinScript.isNotEmpty) {
      final next = pinScript.removeAt(0);
      if (next is ApiException) throw next;
      return next as ContractSignatureResult;
    }
    return signedResult;
  }
}

const _approvedApplication = LoanApplicationResult(
  id: 'app-1',
  status: LoanApplicationStatus.approved,
  decisionReason: 'Score y capacidad de pago dentro de política.',
  requestedAmountMinor: 1000000,
  termMonths: 12,
  totalIncomeMinor: 350000,
  totalDebtMinor: 0,
);

Future<String> _fakeSign(String nonce) async => 'a' * 64;

Future<void> _pumpContract(
  WidgetTester tester,
  FakeCreditContractService fake, {
  LoanApplicationResult? application,
  String? applicationId,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: CreditContractPage(
        service: fake,
        application: application ?? _approvedApplication,
        applicationId: applicationId,
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _pumpSign(
  WidgetTester tester, {
  required FakeCreditContractService fake,
  required FakeBiometricReader reader,
  SignChallenge? challenge,
  bool skipChallenge = false,
  String? applicationId,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: CreditSignPage(
        service: fake,
        reader: reader,
        signNonce: _fakeSign,
        deviceId: 'dev-1',
        applicationId: applicationId ?? 'app-1',
        summary: 'Personal - S/ 10,000 - 12 m - S/ 927.50/mes',
        initialChallenge: skipChallenge
            ? null
            : (challenge ??
                const SignChallenge(
                  applicationId: 'app-1',
                  nonce: 'nonce-1',
                  expiresIn: 120,
                  status: 'GENERATED',
                )),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _consent(WidgetTester tester) async {
  await tester.tap(find.byKey(const Key('sign-consent')));
  await tester.pumpAndSettle();
}

/// Desplaza el `ListView` hasta que [finder] sea visible (la pagina de firma
/// excede el viewport de 800x600: sin esto los taps bajo el pliegue fallan).
Future<void> _reveal(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(
    finder,
    400,
    scrollable: find.byType(Scrollable).first,
  );
  await tester.pumpAndSettle();
}

Future<void> _tapRevealed(WidgetTester tester, Finder finder) async {
  await _reveal(tester, finder);
  await tester.tap(finder);
  await tester.pumpAndSettle();
}

/// Lleva el `ListView` al final: la cola (exito/error/nota) es perezosa y
/// solo existe en el arbol tras el scroll (igual que en uso real).
Future<void> _scrollToEnd(WidgetTester tester) async {
  await tester.drag(
    find.byKey(const Key('sign-content')),
    const Offset(0, -5000),
  );
  await tester.pumpAndSettle();
}

void main() {
  // --pantalla_de_contrato------------------------------------------------------

  group('CreditContractPage', () {
    testWidgets('muestra los 3 documentos y el hero del servidor',
        (tester) async {
      final fake = FakeCreditContractService();
      await _pumpContract(tester, fake);

      expect(find.byKey(const Key('contract-content')), findsOneWidget);
      // Hero con montos del mock (cliente delgado: sin recalcular).
      expect(find.text('S/ 10,000.00 • 12 meses'), findsOneWidget);
      // Los 3 documentos con Abrir (`Contrato de crédito` tambien es el
      // titulo del AppBar: la fila se verifica por clave).
      expect(find.byKey(const Key('contract-doc-0')), findsOneWidget);
      expect(find.text('Pagaré'), findsOneWidget);
      expect(find.text('Hoja resumen'), findsOneWidget);
      expect(find.byKey(const Key('contract-open-contrato')), findsOneWidget);
      expect(find.byKey(const Key('contract-open-pagare')), findsOneWidget);
      expect(find.byKey(const Key('contract-open-resumen')), findsOneWidget);
      // La cola (CTAs) es perezosa: scroll antes de verificarla.
      await tester.drag(
        find.byKey(const Key('contract-content')),
        const Offset(0, -5000),
      );
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('contract-sign-cta')), findsOneWidget);
      expect(
        find.byKey(const Key('contract-download-cta')),
        findsOneWidget,
      );
      expect(fake.getCalls, 1);
    });

    testWidgets('vacio sin documentos generados', (tester) async {
      final fake = FakeCreditContractService()
        ..bundle = ContractBundle.empty(applicationId: 'app-1');
      await _pumpContract(tester, fake);

      expect(find.byKey(const Key('contract-empty')), findsOneWidget);
      expect(find.byKey(const Key('contract-content')), findsNothing);
    });

    testWidgets('error con Reintentar y luego contenido', (tester) async {
      final fake = FakeCreditContractService()
        ..bundleError = ApiException(
          code: 'NOT_FOUND',
          message: 'No encontramos el contrato.',
        );
      await _pumpContract(tester, fake);

      expect(find.byKey(const Key('contract-error')), findsOneWidget);
      expect(find.textContaining('No encontramos el contrato'), findsOneWidget);

      fake.bundleError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('contract-content')), findsOneWidget);
      expect(fake.getCalls, 2);
    });

    testWidgets('Abrir descarga el PDF del servidor', (tester) async {
      final fake = FakeCreditContractService();
      await _pumpContract(tester, fake);

      await tester.tap(find.byKey(const Key('contract-open-contrato')));
      await tester.pumpAndSettle();

      expect(fake.downloadCalls, ['contrato']);
      expect(find.textContaining('contrato.pdf'), findsOneWidget);
    });

    testWidgets('descarga 501 muestra mensaje sin romper', (tester) async {
      final fake = FakeCreditContractService()
        ..downloadError = ApiException(
          code: 'EXPORT_FORMAT_NOT_SUPPORTED',
          message: 'Exportación no disponible por ahora.',
          statusCode: 501,
        );
      await _pumpContract(tester, fake);

      await tester.tap(find.byKey(const Key('contract-open-pagare')));
      await tester.pumpAndSettle();

      expect(
        find.text('Exportación no disponible por ahora.'),
        findsOneWidget,
      );
    });
  });

  // --pantalla_de_firma-----------------------------------------------------------

  group('CreditSignPage', () {
    testWidgets('camino feliz: consentimiento -> biometria -> SIGNED',
        (tester) async {
      final fake = FakeCreditContractService();
      final reader = FakeBiometricReader();
      await _pumpSign(tester, fake: fake, reader: reader);

      // Sin consentimiento el CTA esta bloqueado.
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('sign-biometric-cta')))
            .onPressed,
        isNull,
      );

      await _consent(tester);
      await _tapRevealed(tester, find.byKey(const Key('sign-biometric-cta')));
      await tester.pumpAndSettle();

      expect(reader.authenticateCalls, 1);
      expect(fake.bioCalls, 1);
      await _scrollToEnd(tester);
      expect(find.byKey(const Key('sign-success')), findsOneWidget);
      expect(find.text('¡Documento firmado!'), findsOneWidget);
      expect(
        find.text(
          'Contrato firmado. Copia certificada enviada a tu correo.',
        ),
        findsOneWidget,
      );
      expect(
        find.textContaining('Sello doc_hash verificado'),
        findsOneWidget,
      );
    });

    testWidgets('biometria cancelada: no se envia la firma', (tester) async {
      final fake = FakeCreditContractService();
      final reader = FakeBiometricReader(succeeds: false);
      await _pumpSign(tester, fake: fake, reader: reader);

      await _consent(tester);
      await _tapRevealed(tester, find.byKey(const Key('sign-biometric-cta')));
      await tester.pumpAndSettle();

      expect(fake.bioCalls, 0);
      expect(find.byKey(const Key('sign-success')), findsNothing);
      expect(
        find.text('No pudimos verificar tu biometría. Reintenta o usa tu PIN.'),
        findsOneWidget,
      );
      expect(find.byKey(const Key('sign-retry')), findsOneWidget);
    });

    testWidgets('sin biometria disponible: fallback directo a PIN',
        (tester) async {
      final fake = FakeCreditContractService();
      final reader = FakeBiometricReader(available: false);
      await _pumpSign(tester, fake: fake, reader: reader);

      await _consent(tester);
      await _tapRevealed(tester, find.byKey(const Key('sign-biometric-cta')));
      await tester.pumpAndSettle();

      expect(fake.bioCalls, 0);
      expect(
        find.text(
          'Este dispositivo no tiene biometría disponible. Continúa con tu PIN.',
        ),
        findsOneWidget,
      );
    });

    testWidgets('EXPIRED_NONCE rota el reto y reintenta con clave nueva',
        (tester) async {
      final fake = FakeCreditContractService()
        ..bioScript.add(
          ApiException(code: 'EXPIRED_NONCE', message: 'Reto vencido.'),
        )
        ..challenge = const SignChallenge(
          applicationId: 'app-1',
          nonce: 'nonce-2',
          expiresIn: 120,
          status: 'GENERATED',
        );
      final reader = FakeBiometricReader();
      await _pumpSign(tester, fake: fake, reader: reader);

      await _consent(tester);
      await _tapRevealed(tester, find.byKey(const Key('sign-biometric-cta')));
      await tester.pumpAndSettle();

      expect(fake.bioCalls, 2);
      expect(fake.challengeCalls, 1);
      expect(fake.bioKeys[0], isNotNull);
      expect(fake.bioKeys[1], isNotNull);
      expect(fake.bioKeys[0], isNot(fake.bioKeys[1]));
      expect(fake.bioBodies[0]['nonce'], 'nonce-1');
      expect(fake.bioBodies[1]['nonce'], 'nonce-2');
      await _scrollToEnd(tester);
      expect(find.byKey(const Key('sign-success')), findsOneWidget);
      expect(
        find.text('Se generó un nuevo reto y se completó la firma.'),
        findsOneWidget,
      );
    });

    testWidgets('reintento manual reutiliza la misma Idempotency-Key',
        (tester) async {
      final fake = FakeCreditContractService()
        ..bioScript.add(ApiException.network());
      final reader = FakeBiometricReader();
      await _pumpSign(tester, fake: fake, reader: reader);

      await _consent(tester);
      await _tapRevealed(tester, find.byKey(const Key('sign-biometric-cta')));
      await tester.pumpAndSettle();
      await _scrollToEnd(tester);
      expect(find.byKey(const Key('sign-error-card')), findsOneWidget);

      await _tapRevealed(tester, find.byKey(const Key('sign-retry')));
      await tester.pumpAndSettle();

      expect(fake.bioCalls, 2);
      expect(fake.bioKeys[0], fake.bioKeys[1]);
      await _scrollToEnd(tester);
      expect(find.byKey(const Key('sign-success')), findsOneWidget);
    });

    testWidgets('CONFLICT se mapea a ya firmado', (tester) async {
      final fake = FakeCreditContractService()
        ..bioScript.add(
          ApiException(code: 'CONFLICT', message: 'Ya firmado.'),
        );
      final reader = FakeBiometricReader();
      await _pumpSign(tester, fake: fake, reader: reader);

      await _consent(tester);
      await _tapRevealed(tester, find.byKey(const Key('sign-biometric-cta')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('sign-success')), findsNothing);
      expect(
        find.text('Este contrato ya fue firmado. Revisa tu correo.'),
        findsOneWidget,
      );
      await _scrollToEnd(tester);
      expect(find.byKey(const Key('sign-error-card')), findsOneWidget);
    });

    testWidgets('fallback PIN: firma con 6 digitos', (tester) async {
      final fake = FakeCreditContractService();
      final reader = FakeBiometricReader();
      await _pumpSign(tester, fake: fake, reader: reader);

      await _consent(tester);
      await _reveal(tester, find.byKey(const Key('sign-pin-input')));
      await tester.enterText(
        find.byKey(const Key('sign-pin-input')),
        '123456',
      );
      await tester.pumpAndSettle();
      await _tapRevealed(tester, find.byKey(const Key('sign-pin-cta')));
      await tester.pumpAndSettle();

      expect(fake.pinCalls, 1);
      expect(fake.bioCalls, 0);
      await _scrollToEnd(tester);
      expect(find.byKey(const Key('sign-success')), findsOneWidget);
    });

    testWidgets('PIN con formato invalido no se envia', (tester) async {
      final fake = FakeCreditContractService();
      final reader = FakeBiometricReader();
      await _pumpSign(tester, fake: fake, reader: reader);

      await _consent(tester);
      await _reveal(tester, find.byKey(const Key('sign-pin-input')));
      await tester.enterText(find.byKey(const Key('sign-pin-input')), '123');
      await tester.pumpAndSettle();
      await _tapRevealed(tester, find.byKey(const Key('sign-pin-cta')));
      await tester.pumpAndSettle();

      expect(fake.pinCalls, 0);
      expect(
        find.text('PIN incorrecto. Verifica e intenta de nuevo.'),
        findsOneWidget,
      );
    });

    testWidgets('PIN rechazado muestra mensaje sin oraculo', (tester) async {
      final fake = FakeCreditContractService()
        ..pinScript.add(
          ApiException(code: 'NOT_AUTHORIZED', message: 'Firma invalida.'),
        );
      final reader = FakeBiometricReader();
      await _pumpSign(tester, fake: fake, reader: reader);

      await _consent(tester);
      await _reveal(tester, find.byKey(const Key('sign-pin-input')));
      await tester.enterText(
        find.byKey(const Key('sign-pin-input')),
        '654321',
      );
      await tester.pumpAndSettle();
      await _tapRevealed(tester, find.byKey(const Key('sign-pin-cta')));
      await tester.pumpAndSettle();

      expect(fake.pinCalls, 1);
      expect(find.byKey(const Key('sign-success')), findsNothing);
      expect(
        find.text('PIN incorrecto. Verifica e intenta de nuevo.'),
        findsOneWidget,
      );
    });

    testWidgets('reto ya SIGNED muestra exito directo', (tester) async {
      final fake = FakeCreditContractService();
      final reader = FakeBiometricReader();
      await _pumpSign(
        tester,
        fake: fake,
        reader: reader,
        challenge: const SignChallenge(
          applicationId: 'app-1',
          nonce: '',
          expiresIn: 0,
          status: 'SIGNED',
        ),
      );

      await _scrollToEnd(tester);
      expect(find.byKey(const Key('sign-success')), findsOneWidget);
      expect(
        find.text('Este contrato ya fue firmado. Revisa tu correo.'),
        findsOneWidget,
      );
      expect(fake.bioCalls, 0);
    });

    testWidgets('error del reto con Reintentar', (tester) async {
      final fake = FakeCreditContractService()
        ..challengeError = ApiException.network();
      final reader = FakeBiometricReader();
      await _pumpSign(
        tester,
        fake: fake,
        reader: reader,
        skipChallenge: true,
      );

      expect(find.byKey(const Key('sign-error')), findsOneWidget);

      fake.challengeError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('sign-content')), findsOneWidget);
    });

    testWidgets('sin expediente muestra vacio', (tester) async {
      final fake = FakeCreditContractService();
      final reader = FakeBiometricReader();
      await _pumpSign(
        tester,
        fake: fake,
        reader: reader,
        skipChallenge: true,
        applicationId: '',
      );

      expect(find.byKey(const Key('sign-empty')), findsOneWidget);
    });

    testWidgets('el resumen muestra los valores del servidor tal cual',
        (tester) async {
      final fake = FakeCreditContractService();
      final reader = FakeBiometricReader();
      await _pumpSign(tester, fake: fake, reader: reader);

      // Cliente delgado: el texto es el del mock del backend, sin recalculo.
      expect(
        find.text('Personal - S/ 10,000 - 12 m - S/ 927.50/mes'),
        findsOneWidget,
      );
    });
  });

  // --controlador_y_mensajes--------------------------------------------------------

  group('CreditSignController', () {
    CreditSignController controllerWith(
      FakeCreditContractService fake,
      FakeBiometricReader reader,
    ) {
      return CreditSignController(
        service: fake,
        reader: reader,
        signNonce: _fakeSign,
        deviceId: 'dev-1',
      );
    }

    const challenge = SignChallenge(
      applicationId: 'app-1',
      nonce: 'nonce-1',
      expiresIn: 120,
      status: 'GENERATED',
    );

    test('feliz: firma y devuelve SIGNED en 1 intento', () async {
      final fake = FakeCreditContractService();
      final controller = controllerWith(fake, FakeBiometricReader());

      final state = await controller.signWithBiometrics(
        applicationId: 'app-1',
        challenge: challenge,
        idempotencyKey: 'key-1',
        reason: 'Firma',
      );

      expect(state.outcome, CreditSignOutcome.signed);
      expect(state.attempts, 1);
      expect(state.result?.isSigned, isTrue);
      expect(fake.bioKeys, ['key-1']);
    });

    test('nonce vacio no se firma (expired, sin red)', () async {
      final fake = FakeCreditContractService();
      final controller = CreditSignController(
        service: fake,
        reader: FakeBiometricReader(),
        signNonce: (_) async => throw ArgumentError('vacio'),
        deviceId: 'dev-1',
      );

      final state = await controller.signWithBiometrics(
        applicationId: 'app-1',
        challenge: challenge,
        idempotencyKey: 'key-1',
        reason: 'Firma',
      );

      expect(state.outcome, CreditSignOutcome.expired);
      expect(fake.bioCalls, 0);
    });

    test('doble EXPIRED_NONCE agota el reintento', () async {
      final fake = FakeCreditContractService()
        ..bioScript.addAll([
          ApiException(code: 'EXPIRED_NONCE', message: 'Vencido.'),
          ApiException(code: 'EXPIRED_NONCE', message: 'Vencido.'),
        ]);
      final controller = controllerWith(fake, FakeBiometricReader());

      final state = await controller.signWithBiometrics(
        applicationId: 'app-1',
        challenge: challenge,
        idempotencyKey: 'key-1',
        reason: 'Firma',
      );

      expect(state.outcome, CreditSignOutcome.expired);
      expect(state.attempts, 2);
    });

    test('NOT_FOUND y VALIDATION_ERROR se mapean en espanol', () {
      expect(
        creditContractUserMessage(
          ApiException(code: 'NOT_FOUND', message: 'x'),
        ),
        contains('No encontramos el contrato'),
      );
      expect(
        creditContractUserMessage(
          ApiException(code: 'VALIDATION_ERROR', message: 'x'),
        ),
        contains('Revisa los datos'),
      );
      expect(
        creditContractUserMessage(
          ApiException(code: 'EXPIRED_NONCE', message: 'x'),
        ),
        contains('nuevo reto'),
      );
    });

    test('creditSignRetryKey deriva clave nueva y respeta vacio', () {
      expect(creditSignRetryKey('abc'), 'abc-retry-1');
      expect(creditSignRetryKey(null), isNull);
      expect(creditSignRetryKey('  '), isNull);
      final long = 'k' * 80;
      expect(creditSignRetryKey(long)!.length, 80);
    });

    test('isCreditSignConflictCode reconoce los 409', () {
      expect(isCreditSignConflictCode('CONFLICT'), isTrue);
      expect(isCreditSignConflictCode('IDEMPOTENCY_CONFLICT'), isTrue);
      expect(isCreditSignConflictCode('EXPIRED_NONCE'), isFalse);
    });
  });
}
