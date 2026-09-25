import 'dart:typed_data';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/core/widgets/app_button.dart';
import 'package:banca_online/features/kyc/kyc_error_handler.dart';
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_document_page.dart';
import 'package:banca_online/features/kyc/presentation/kyc_result_page.dart';
import 'package:banca_online/features/kyc/presentation/kyc_start_page.dart';
import 'package:banca_online/features/kyc/presentation/kyc_task_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Reinicio total del registro KYC (F-T47, decisión del dueño).
///
/// - Fallo final (submit sin resultado que permita continuar u 3 intentos
///   agotados) => `resetFull()` SILENCIOSO + `/kyc`, sin popup.
/// - Retroceso voluntario en `/kyc/document`, `/kyc/task` y `/kyc/result` =>
///   popup de confirmación; confirmar reinicia todo y va a `/kyc`, cancelar
///   se queda sin cambios.
/// - La sesión (`user.ref`/`device.id`) no se toca.
///
/// Esta prueba FALLA con el código anterior a F-T47: `reset()` no borraba
/// tipo/número de documento ni titular, no existía señal de reinicio para el
/// estado local de `KycStartPage` y el fallo final no reiniciaba.
class _FakeKycService implements KycService {
  _FakeKycService({
    this.submitResult = const KycSubmitResult(
      overallResult: true,
      detailCode: 'OK',
      userId: 'u-42',
    ),
  });

  KycSubmitResult submitResult;
  Object? throwOnSubmit;

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
  }) async {
    final error = throwOnSubmit;
    if (error != null) throw error;
    return submitResult;
  }
}

/// Doble del lookup de titular (F-T44): sin red ni PII real.
class _FakeDocumentLookup implements KycDocumentLookupService {
  const _FakeDocumentLookup();

  @override
  Future<KycDocumentOwner> lookupDocument({
    required String type,
    required String number,
  }) async =>
      const KycDocumentOwner(
        documentType: 'DNI',
        firstName: 'Ana',
        lastName: 'Perez',
      );
}

const _applicant = KycApplicant(
  firstName: 'Ana',
  lastName: 'Perez',
  email: 'ana@example.com',
  phone: '999888777',
);

/// Deja el controller con estado COMPLETO de una corrida (challenge,
/// documento+titular, captura de documento, tareas pasadas y submit exitoso).
Future<void> _fillFullRun(KycFlowController c) async {
  await c.loadChallenge();
  c.setDocument(type: 'RUC', number: '12345678901');
  c.setApplicant(_applicant);
  await c.captureDocument();
  await c.captureAndResolveCurrentTask(); // front
  await c.captureAndResolveCurrentTask(); // blink
  await c.submit();
}

void _expectFullyCleared(KycFlowController c) {
  expect(c.challenge, isNull, reason: 'sin challenge');
  expect(c.token, isNull);
  expect(c.steps, isEmpty);
  expect(c.documentType, 'DNI', reason: 'tipo vuelve a DNI');
  expect(c.documentNumber, isEmpty, reason: 'número borrado');
  expect(c.applicant, isNull, reason: 'titular borrado (incluye email/tel)');
  expect(c.result, isNull, reason: 'sin resultado previo');
  expect(c.errorMessage, isNull);
  expect(c.lastError, isNull);
  expect(c.manualReviewFolio, isNull, reason: 'sin folio de revisión');
  expect(c.documentImage, isNull);
  expect(c.documentValidation, isNull);
  expect(c.documentIssues, isEmpty);
  expect(c.documentValidationError, isNull);
  expect(c.documentAttempts, 0);
  expect(c.currentStepIndex, 0);
  expect(c.needsManualReviewAny, isFalse);
  expect(c.isFinalFailure, isFalse);
}

void main() {
  group('F-T47 controller: resetFull borra TODO', () {
    test('resetea challenge, documento, titular, tareas y resultado + señal',
        () async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await _fillFullRun(c);

      expect(c.challenge, isNotNull);
      expect(c.documentType, 'RUC');
      expect(c.documentNumber, '12345678901');
      expect(c.applicant?.email, 'ana@example.com');
      expect(c.result?.overallResult, isTrue);
      expect(c.resetGeneration, 0);

      c.resetFull();

      _expectFullyCleared(c);
      expect(c.resetGeneration, 1, reason: 'emite la señal de reinicio');
    });

    test('reset() conserva tipo/numero/titular (contrato legacy intacto)',
        () async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await c.loadChallenge();
      c.setDocument(type: 'RUC', number: '12345678901');
      c.setApplicant(_applicant);
      await c.captureAndResolveCurrentTask();

      c.reset();

      expect(c.challenge, isNull);
      expect(c.documentType, 'RUC', reason: 'reset() no borra el tipo');
      expect(c.documentNumber, '12345678901',
          reason: 'reset() no borra el número');
      expect(c.applicant?.email, 'ana@example.com',
          reason: 'reset() no borra el titular');
      expect(c.resetGeneration, 1, reason: 'también emite la señal');
    });
  });

  group('F-T47 controller: fallo final vs. recuperable', () {
    test('submit overall_result=false es final y el helper reinicia todo',
        () async {
      final c = KycFlowController(
        service: _FakeKycService(
          submitResult: const KycSubmitResult(
            overallResult: false,
            detailCode: 'LIVENESS_FAILED',
            failedStep: 'blink',
            overallReason: 'INSUFFICIENT_MOVEMENT',
          ),
        ),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      c.setDocument(type: 'RUC', number: '12345678901');
      c.setApplicant(_applicant);
      await c.captureAndResolveCurrentTask();
      await c.captureAndResolveCurrentTask();
      await c.submit();

      expect(c.result?.overallResult, isFalse);
      expect(c.isFinalFailure, isTrue);

      expect(c.resetFullOnFinalFailure(), isTrue);
      _expectFullyCleared(c);
      expect(c.resetGeneration, 1);
    });

    test('3 intentos agotados es final y el helper reinicia todo', () async {
      final c = KycFlowController(
        service: _FakeKycService(),
        taskEvaluator: (task, frames) async => false,
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      c.setDocument(type: 'DNI', number: '12345678');
      c.setApplicant(_applicant);

      await c.captureAndResolveCurrentTask();
      await c.captureAndResolveCurrentTask();
      await c.captureAndResolveCurrentTask();

      // E1-T06 intacto a nivel de controller: hay folio y derivación.
      expect(c.needsManualReviewAny, isTrue);
      expect(c.lastError?.action, KycErrorAction.manualReview);
      expect(c.manualReviewFolio, startsWith('KYC-'));
      expect(c.isFinalFailure, isTrue);

      // F-T47: en el alta eso se convierte en reinicio total silencioso.
      expect(c.resetFullOnFinalFailure(), isTrue);
      _expectFullyCleared(c);
    });

    test('fallo de red en submit NO es final y conserva el estado', () async {
      final service = _FakeKycService()
        ..throwOnSubmit = ApiException.network();
      final c = KycFlowController(service: service);
      addTearDown(c.dispose);
      await c.loadChallenge();
      c.setDocument(type: 'RUC', number: '12345678901');
      c.setApplicant(_applicant);
      await c.captureAndResolveCurrentTask();
      await c.captureAndResolveCurrentTask();
      await c.submit();

      expect(c.result, isNull);
      expect(c.errorMessage, contains('Sin conexion'));
      expect(c.isFinalFailure, isFalse,
          reason: 'la red es recuperable (retrySubmit), no final');
      expect(c.resetFullOnFinalFailure(), isFalse);

      // Estado intacto para reintentar el submit.
      expect(c.token, 'tok-abc');
      expect(c.readyToSubmit, isTrue);
      expect(c.documentNumber, '12345678901');
      expect(c.applicant?.email, 'ana@example.com');
    });

    test('token expirado NO es final (pide refresh conservando progreso)',
        () async {
      var first = true;
      final c = KycFlowController(
        service: _FakeKycService(),
        taskEvaluator: (task, frames) async {
          if (first) {
            first = false;
            throw ApiException(
              code: 'TOKEN_EXPIRED',
              message: 'mensaje base',
              serverMessage: 'challenge token expired',
              statusCode: 401,
            );
          }
          return true;
        },
      );
      addTearDown(c.dispose);
      await c.loadChallenge();
      await c.captureAndResolveCurrentTask();

      expect(c.lastError?.action, KycErrorAction.refreshChallenge);
      expect(c.isFinalFailure, isFalse);
      expect(c.resetFullOnFinalFailure(), isFalse);
      expect(c.token, 'tok-abc', reason: 'se conserva el desafío');
    });

    test('bloqueo de negocio (RISK_BLOCKED) es final', () async {
      final service = _FakeKycService()
        ..throwOnSubmit = ApiException(
          code: 'RISK_BLOCKED',
          message: 'bloqueado',
          statusCode: 403,
        );
      final c = KycFlowController(service: service);
      addTearDown(c.dispose);
      await c.loadChallenge();
      c.setDocument(type: 'DNI', number: '12345678');
      c.setApplicant(_applicant);
      await c.captureAndResolveCurrentTask();
      await c.captureAndResolveCurrentTask();
      await c.submit();

      expect(c.lastError?.action, KycErrorAction.manualReview);
      expect(c.isFinalFailure, isTrue);
      expect(c.resetFullOnFinalFailure(), isTrue);
      _expectFullyCleared(c);
    });
  });

  group('F-T47 controller: sesión y cámara', () {
    test('resetFull no toca la sesión (user.ref intacto)', () async {
      final identity = InMemorySessionIdentityStore(
        deviceId: 'dev-1',
        userRef: 'u-1',
      );
      addTearDown(identity.dispose);
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await _fillFullRun(c);

      c.resetFull();
      expect(c.resetFullOnFinalFailure(), isFalse,
          reason: 'tras limpiar ya no hay fallo final');

      expect(identity.userRef, 'u-1',
          reason: 'el reinicio no borra la sesión');
      expect(await identity.readUserRef(), 'u-1');
    });

    test('resetFull libera best-effort y el flujo puede volver a empezar',
        () async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await c.loadChallenge();
      await c.captureDocument();
      expect(c.hasDocumentImage, isTrue);

      c.resetFull();
      _expectFullyCleared(c);

      // La fuente sigue reutilizable: pedir challenge y capturar de nuevo.
      await c.loadChallenge();
      await c.captureDocument();
      expect(c.hasDocumentImage, isTrue);
      await c.captureAndResolveCurrentTask();
      expect(c.taskPassed('front'), isTrue);
    });
  });

  group('F-T47 widget: KycStartPage limpia su estado local', () {
    String fieldText(WidgetTester tester, String key) =>
        tester
                .widget<TextFormField>(find.byKey(Key(key)))
                .controller
                ?.text ??
            '';

    testWidgets('tras el reinicio no queda email/teléfono/nombres/número',
        (tester) async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: KycStartPage(
            controller: c,
            lookupService: const _FakeDocumentLookup(),
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const Key('docNumberField')),
        '12345678',
      );
      await tester.enterText(
        find.byKey(const Key('emailField')),
        'ana@example.com',
      );
      await tester.enterText(
        find.byKey(const Key('phoneField')),
        '999888777',
      );
      await tester.pump();
      await tester.ensureVisible(find.byKey(const Key('validateDocumentButton')));
      await tester.tap(find.byKey(const Key('validateDocumentButton')));
      await tester.pumpAndSettle();
      expect(fieldText(tester, 'emailField'), 'ana@example.com');
      expect(fieldText(tester, 'phoneField'), '999888777');
      expect(fieldText(tester, 'firstNameField'), 'Ana');

      // Estado del controller de la corrida + reinicio total.
      c.setDocument(type: 'DNI', number: '12345678');
      c.setApplicant(_applicant);
      c.resetFull();
      await tester.pumpAndSettle();

      expect(fieldText(tester, 'emailField'), isEmpty,
          reason: 'el email previo no debe reaparecer');
      expect(fieldText(tester, 'phoneField'), isEmpty,
          reason: 'el teléfono previo no debe reaparecer');
      expect(fieldText(tester, 'docNumberField'), isEmpty,
          reason: 'el número previo no debe reaparecer');
      expect(fieldText(tester, 'firstNameField'), isEmpty,
          reason: 'los nombres previos no deben reaparecer');
      expect(fieldText(tester, 'lastNameField'), isEmpty);
      // Continuar exige revalidar el documento.
      final continuar = tester.widget<AppPrimaryButton>(
        find.widgetWithText(AppPrimaryButton, 'Continuar'),
      );
      expect(continuar.onPressed, isNull);
    });
  });

  group('F-T47 widget: popup de retroceso voluntario', () {
    GoRouter routerFor(KycFlowController c, {String initial = '/kyc'}) =>
        GoRouter(
          initialLocation: initial,
          routes: [
            GoRoute(
              path: '/kyc',
              builder: (context, state) => KycStartPage(
                controller: c,
                lookupService: const _FakeDocumentLookup(),
              ),
            ),
            GoRoute(
              path: '/kyc/document',
              builder: (context, state) =>
                  KycDocumentPage(controller: c),
            ),
            GoRoute(
              path: '/kyc/task',
              builder: (context, state) => KycTaskPage(
                controller: c,
                prepDuration: Duration.zero,
              ),
            ),
            GoRoute(
              path: '/kyc/result',
              builder: (context, state) =>
                  KycResultPage(controller: c),
            ),
          ],
        );

    Future<void> seedProgress(KycFlowController c) async {
      await c.loadChallenge();
      c.setDocument(type: 'RUC', number: '12345678901');
      c.setApplicant(_applicant);
    }

    testWidgets('documento: cancelar se queda, confirmar reinicia a /kyc',
        (tester) async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await seedProgress(c);
      final router = routerFor(c);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();
      expect(find.text('Empecemos por ti'), findsOneWidget);

      router.push('/kyc/document');
      await tester.pumpAndSettle();
      expect(find.text('Escanea tu DNI'), findsOneWidget);

      // Cancelar: el popup se cierra y NO se navega ni se borra nada.
      await tester.tap(find.byKey(const Key('kycBackButton')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('kycBackConfirmDialog')), findsOneWidget);
      expect(
        find.textContaining('se reiniciará todo'),
        findsOneWidget,
        reason: 'aviso claro, sin PII',
      );
      await tester.tap(find.byKey(const Key('kycBackCancelButton')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('kycBackConfirmDialog')), findsNothing);
      expect(find.text('Escanea tu DNI'), findsOneWidget);
      expect(c.challenge, isNotNull);
      expect(c.documentType, 'RUC');
      expect(c.applicant?.email, 'ana@example.com');

      // Confirmar: reinicia TODO y vuelve a `/kyc`, sin PII en el diálogo.
      await tester.tap(find.byKey(const Key('kycBackButton')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('kycBackConfirmButton')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('kycBackConfirmDialog')), findsNothing);
      expect(find.text('Empecemos por ti'), findsOneWidget);
      _expectFullyCleared(c);
    });

    testWidgets('tarea: cancelar se queda, confirmar reinicia a /kyc',
        (tester) async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await seedProgress(c);
      final router = routerFor(c);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      router.push('/kyc/task');
      await tester.pumpAndSettle();
      expect(find.textContaining('Paso 1 de 2'), findsOneWidget);

      await tester.tap(find.byKey(const Key('kycBackButton')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('kycBackConfirmDialog')), findsOneWidget);
      await tester.tap(find.byKey(const Key('kycBackCancelButton')));
      await tester.pumpAndSettle();
      expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
      expect(c.challenge, isNotNull);

      await tester.tap(find.byKey(const Key('kycBackButton')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('kycBackConfirmButton')));
      await tester.pumpAndSettle();
      expect(find.text('Empecemos por ti'), findsOneWidget);
      _expectFullyCleared(c);
    });

    testWidgets('resultado: retroceder confirma y reinicia a /kyc',
        (tester) async {
      final c = KycFlowController(service: _FakeKycService());
      addTearDown(c.dispose);
      await seedProgress(c);
      await c.captureAndResolveCurrentTask();
      await c.captureAndResolveCurrentTask();
      await c.submit();
      expect(c.result?.overallResult, isTrue);
      final router = routerFor(c);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      router.push('/kyc/result');
      await tester.pumpAndSettle();
      expect(find.text('Verificacion exitosa'), findsOneWidget);

      // Retroceso del sistema: también pide confirmación.
      await tester.binding.handlePopRoute();
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('kycBackConfirmDialog')), findsOneWidget);
      await tester.tap(find.byKey(const Key('kycBackCancelButton')));
      await tester.pumpAndSettle();
      expect(find.text('Verificacion exitosa'), findsOneWidget);
      expect(c.result, isNotNull);

      await tester.binding.handlePopRoute();
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('kycBackConfirmButton')));
      await tester.pumpAndSettle();
      expect(find.text('Empecemos por ti'), findsOneWidget);
      _expectFullyCleared(c);
    });
  });

  group('F-T47 widget: fallo final silencioso (sin popup)', () {
    Future<void> completeStartPage(WidgetTester tester) async {
      await tester.enterText(
        find.byKey(const Key('emailField')),
        'ana@example.com',
      );
      await tester.enterText(
        find.byKey(const Key('docNumberField')),
        '12345678',
      );
      await tester.pump();
      await tester.ensureVisible(find.byKey(const Key('validateDocumentButton')));
      await tester.tap(find.byKey(const Key('validateDocumentButton')));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Continuar'));
      await tester.tap(find.text('Continuar'));
      await tester.pumpAndSettle();
    }

    Future<void> captureDocumentAndContinue(WidgetTester tester) async {
      expect(find.text('Escanea tu DNI'), findsOneWidget);
      await tester.tap(find.byKey(const Key('captureDocumentButton')));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.byKey(const Key('kycDocumentContinue')));
      await tester.tap(find.byKey(const Key('kycDocumentContinue')));
      await tester.pumpAndSettle();
    }

    Future<void> tapTaskButton(WidgetTester tester, String label) async {
      await tester.ensureVisible(find.text(label));
      await tester.tap(find.text(label));
      await tester.pumpAndSettle();
    }

    /// Texto real del campo (el hint `12345678` seguiría visible en vacío).
    String fieldText(WidgetTester tester, String key) =>
        tester
                .widget<TextFormField>(find.byKey(Key(key)))
                .controller
                ?.text ??
            '';

    GoRouter failingSubmitRouter(KycFlowController c) => GoRouter(
          initialLocation: '/kyc',
          routes: [
            GoRoute(
              path: '/kyc',
              builder: (context, state) => KycStartPage(
                controller: c,
                lookupService: const _FakeDocumentLookup(),
              ),
            ),
            GoRoute(
              path: '/kyc/document',
              builder: (context, state) =>
                  KycDocumentPage(controller: c),
            ),
            GoRoute(
              path: '/kyc/task',
              builder: (context, state) => KycTaskPage(
                controller: c,
                prepDuration: Duration.zero,
              ),
            ),
            GoRoute(
              path: '/kyc/result',
              builder: (context, state) =>
                  KycResultPage(controller: c),
            ),
          ],
        );

    testWidgets('submit overall_result=false vuelve a /kyc sin popup',
        (tester) async {
      final c = KycFlowController(
        service: _FakeKycService(
          submitResult: const KycSubmitResult(
            overallResult: false,
            detailCode: 'LIVENESS_FAILED',
            failedStep: 'blink',
            overallReason: 'INSUFFICIENT_MOVEMENT',
          ),
        ),
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: true),
      );
      addTearDown(c.dispose);
      final router = failingSubmitRouter(c);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await completeStartPage(tester);
      await captureDocumentAndContinue(tester);
      await tapTaskButton(tester, 'Capturar');
      await tapTaskButton(tester, 'Capturar');
      await tapTaskButton(tester, 'Enviar verificación');

      // Reinicio total silencioso: de vuelta al inicio, sin diálogo.
      expect(find.byKey(const Key('kycBackConfirmDialog')), findsNothing);
      expect(find.text('Empecemos por ti'), findsOneWidget);
      expect(fieldText(tester, 'emailField'), isEmpty,
          reason: 'email/teléfono se borran (evidencia CA-02)');
      expect(fieldText(tester, 'phoneField'), isEmpty);
      expect(fieldText(tester, 'docNumberField'), isEmpty);
      expect(fieldText(tester, 'firstNameField'), isEmpty);
      _expectFullyCleared(c);
      // Sin revalidar el documento no se puede continuar.
      final continuar = tester.widget<AppPrimaryButton>(
        find.widgetWithText(AppPrimaryButton, 'Continuar'),
      );
      expect(continuar.onPressed, isNull);
    });

    testWidgets('3 intentos agotados reinician en silencio a /kyc',
        (tester) async {
      final c = KycFlowController(
        service: _FakeKycService(),
        taskEvaluator: (task, frames) async => false,
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: true),
      );
      addTearDown(c.dispose);
      final router = failingSubmitRouter(c);
      addTearDown(router.dispose);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      await completeStartPage(tester);
      await captureDocumentAndContinue(tester);
      await tapTaskButton(tester, 'Capturar');
      expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
      await tapTaskButton(tester, 'Reintentar captura');
      expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
      await tapTaskButton(tester, 'Reintentar captura');

      // Tercer intento agotado: reinicio silencioso, sin folio ni popup.
      expect(find.byKey(const Key('kycBackConfirmDialog')), findsNothing);
      expect(find.text('Empecemos por ti'), findsOneWidget);
      _expectFullyCleared(c);
    });
  });
}
