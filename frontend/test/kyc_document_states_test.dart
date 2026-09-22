import 'dart:async';
import 'dart:typed_data';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/widgets/app_button.dart';
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_document_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

class _FakeKycService implements KycService {
  const _FakeKycService();

  @override
  Future<KycChallenge> challenge() async => const KycChallenge(
        token: 'tok-abc',
        steps: ['front', 'blink'],
        expiresIn: 300,
      );

  @override
  Future<KycSubmitResult> submit({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required KycApplicant applicant,
    required Map<String, List<Uint8List>> framesByTask,
  }) async =>
      const KycSubmitResult(overallResult: true, detailCode: 'OK');
}

GoRouter _router(KycFlowController c) => GoRouter(
      initialLocation: '/kyc/document',
      routes: [
        GoRoute(
          path: '/kyc/document',
          builder: (context, state) => KycDocumentPage(controller: c),
        ),
        GoRoute(
          path: '/kyc/task',
          builder: (context, state) =>
              const Scaffold(body: Text('kyc-task-ok')),
        ),
      ],
    );

Future<void> _pumpDocument(WidgetTester tester, KycFlowController c) async {
  await tester.pumpWidget(
    MaterialApp.router(routerConfig: _router(c)),
  );
  await tester.pumpAndSettle();
}

Future<void> _tapCapture(WidgetTester tester) async {
  await tester.ensureVisible(find.byKey(const Key('captureDocumentButton')));
  await tester.tap(find.byKey(const Key('captureDocumentButton')));
  await tester.pumpAndSettle();
}

Future<void> _tapContinue(WidgetTester tester) async {
  await tester.ensureVisible(find.byKey(const Key('kycDocumentContinue')));
  await tester.tap(find.byKey(const Key('kycDocumentContinue')));
  await tester.pumpAndSettle();
}

AppPrimaryButton _continuar(WidgetTester tester) => tester.widget<AppPrimaryButton>(
      find.widgetWithText(AppPrimaryButton, 'Continuar'),
    );

void main() {
  group('F-T45 fail-closed (CA-01)', () {
    test('sin validador ni servicio no se acepta el documento', () async {
      final c = KycFlowController(service: const _FakeKycService());
      addTearDown(c.dispose);
      await c.loadChallenge();
      await c.captureDocument();
      expect(c.hasDocumentImage, isTrue);

      final ok = await c.validateDocument();

      expect(ok, isFalse);
      expect(c.documentIsValid, isFalse);
      expect(c.documentValidation?.isValid, isFalse);
      expect(c.documentIssues, isNotEmpty);
    });

    testWidgets('is_valid=false nunca navega a /kyc/task', (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: false, issues: ['BLURRY']),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      await _tapCapture(tester);
      await _tapContinue(tester);

      expect(find.text('Escanea tu DNI'), findsOneWidget);
      expect(find.text('kyc-task-ok'), findsNothing);
      expect(find.byKey(const Key('kycDocumentIssues')), findsOneWidget);
    });

    testWidgets('error de red nunca navega y conserva la captura',
        (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async => throw ApiException.network(),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      await _tapCapture(tester);
      await _tapContinue(tester);

      expect(find.text('Escanea tu DNI'), findsOneWidget);
      expect(find.text('kyc-task-ok'), findsNothing);
      expect(c.hasDocumentImage, isTrue);
      expect(
        find.text('Sin conexión. Revisa tu internet e inténtalo de nuevo.'),
        findsOneWidget,
      );
    });

    testWidgets('la captura sola no navega (sin pulsar Continuar)',
        (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: true),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      await _tapCapture(tester);

      expect(find.text('Escanea tu DNI'), findsOneWidget);
      expect(find.text('kyc-task-ok'), findsNothing);
      expect(find.text('Foto capturada'), findsOneWidget);
    });

    testWidgets('is_valid=true via Continuar si navega', (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: true),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      await _tapCapture(tester);
      await _tapContinue(tester);

      expect(find.text('kyc-task-ok'), findsOneWidget);
    });
  });

  group('F-T45 estados del fig (CA-02..CA-05)', () {
    testWidgets('sin captura: solo Tomar foto, sin Continuar', (tester) async {
      final c = KycFlowController(service: const _FakeKycService());
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      expect(find.text('Escanea tu DNI'), findsOneWidget);
      expect(find.text('Frente del DNI'), findsOneWidget);
      expect(find.text('Foto y datos personales'), findsOneWidget);
      expect(find.text('Tomar foto'), findsOneWidget);
      expect(find.text('0 de 2 capturas'), findsOneWidget);
      expect(find.text('Continuar'), findsNothing);
      expect(find.byKey(const Key('kycDocumentContinue')), findsNothing);
    });

    testWidgets('Capturado: Continuar habilitado, sin bytes ni Volver',
        (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: true),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      await _tapCapture(tester);

      expect(find.text('Foto capturada'), findsOneWidget);
      expect(find.text('1 de 2 capturas'), findsOneWidget);
      expect(find.textContaining('bytes'), findsNothing);
      expect(find.text('Volver a tomar'), findsNothing);
      expect(find.text('Continuar'), findsOneWidget);
      expect(_continuar(tester).onPressed, isNotNull);
    });

    testWidgets('Validando: Continuar bloqueado + indicador', (tester) async {
      final gate = Completer<KycDocumentValidation>();
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) => gate.future,
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      await _tapCapture(tester);
      await tester.ensureVisible(find.byKey(const Key('kycDocumentContinue')));
      await tester.tap(find.byKey(const Key('kycDocumentContinue')));
      await tester.pump();

      expect(find.text('Validando documento...'), findsOneWidget);
      expect(find.text('Foto capturada'), findsOneWidget);
      expect(_continuar(tester).onPressed, isNull);

      gate.complete(const KycDocumentValidation(isValid: true));
      await tester.pumpAndSettle();
      expect(find.text('kyc-task-ok'), findsOneWidget);
    });

    testWidgets(
        'No legible: badge, preview de advertencia, IssuesCard y Volver; '
        'Continuar bloqueado y sin consejos', (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async => const KycDocumentValidation(
          isValid: false,
          issues: ['BLURRY'],
        ),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      await _tapCapture(tester);
      await _tapContinue(tester);

      expect(find.text('No legible'), findsOneWidget);
      expect(find.text('Foto con problemas de lectura'), findsOneWidget);
      expect(find.text('No pudimos leer tu DNI'), findsOneWidget);
      expect(find.byKey(const Key('kycDocumentIssues')), findsOneWidget);
      expect(find.text('Volver a tomar'), findsOneWidget);
      expect(find.textContaining('Consejos para una buena captura'),
          findsNothing);
      expect(find.textContaining('bytes'), findsNothing);
      expect(_continuar(tester).onPressed, isNull);
    });

    testWidgets(
        'Error de red: ErrorBox + Reintentar, sin Volver ni bytes; '
        'Continuar bloqueado', (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async => throw ApiException.network(),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      await _tapCapture(tester);
      await _tapContinue(tester);

      expect(find.text('Foto capturada'), findsOneWidget);
      expect(
        find.text('Sin conexión. Revisa tu internet e inténtalo de nuevo.'),
        findsOneWidget,
      );
      expect(find.text('Reintentar validación'), findsOneWidget);
      expect(find.text('Volver a tomar'), findsNothing);
      expect(find.textContaining('bytes'), findsNothing);
      expect(_continuar(tester).onPressed, isNull);
    });
  });

  group('F-T45 maximo 2 intentos (CA-07)', () {
    testWidgets('tras 2 fallos queda BLOQUEADO en No legible sin avance',
        (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async => const KycDocumentValidation(
          isValid: false,
          issues: ['BLURRY'],
        ),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await _pumpDocument(tester, c);

      // Intento 1: captura + Continuar -> No legible (1 de 2).
      await _tapCapture(tester);
      await _tapContinue(tester);
      expect(find.text('1 de 2 capturas'), findsOneWidget);
      expect(find.byKey(const Key('kycDocumentIssues')), findsOneWidget);
      expect(c.documentBlocked, isFalse);

      // Reintento: Volver a tomar + segunda captura + Continuar.
      await tester.ensureVisible(
          find.byKey(const Key('recaptureDocumentButton')));
      await tester.tap(find.byKey(const Key('recaptureDocumentButton')));
      await tester.pumpAndSettle();
      await _tapCapture(tester);
      await _tapContinue(tester);

      // Bloqueado: 2 de 2, No legible con IssuesCard, sin avance.
      expect(find.text('2 de 2 capturas'), findsOneWidget);
      expect(find.text('Escanea tu DNI'), findsOneWidget);
      expect(find.text('kyc-task-ok'), findsNothing);
      expect(find.text('No legible'), findsOneWidget);
      expect(find.text('Foto con problemas de lectura'), findsOneWidget);
      expect(find.byKey(const Key('kycDocumentIssues')), findsOneWidget);
      expect(c.documentBlocked, isTrue);
      expect(_continuar(tester).onPressed, isNull);

      // Sin avance posible: Continuar sigue bloqueado y no navega.
      await tester.ensureVisible(find.byKey(const Key('kycDocumentContinue')));
      await tester.tap(find.byKey(const Key('kycDocumentContinue')),
          warnIfMissed: false);
      await tester.pumpAndSettle();
      expect(find.text('kyc-task-ok'), findsNothing);

      // Bloqueado: Volver a tomar deshabilitado (no hay tercer intento).
      final recapture = tester.widget<AppPrimaryButton>(
        find.byKey(const Key('recaptureDocumentButton')),
      );
      expect(recapture.onPressed, isNull);
      expect(c.documentAttempts, KycFlowController.maxDocumentAttempts);
    });

    test('el contador no supera el maximo aunque se intente capturar de mas',
        () async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async => const KycDocumentValidation(
          isValid: false,
          issues: ['BLURRY'],
        ),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();

      await c.captureDocument();
      await c.validateDocument();
      c.startDocumentRecapture();
      await c.captureDocument();
      await c.validateDocument();

      expect(c.documentAttempts, 2);
      expect(c.documentBlocked, isTrue);

      // Tercer intento bloqueado: no suma ni limpia el bloqueo.
      c.startDocumentRecapture();
      await c.captureDocument();
      expect(c.documentAttempts, 2);
      expect(c.documentBlocked, isTrue);
      expect(await c.validateDocument(), isFalse);
    });
  });
}
