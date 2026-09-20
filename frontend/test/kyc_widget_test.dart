import 'dart:typed_data';

import 'package:banca_online/core/app_version.dart';
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/core/widgets/app_version_label.dart';
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

class FakeKycService implements KycService {
  const FakeKycService();

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
      const KycSubmitResult(
        overallResult: true,
        detailCode: 'OK',
        userId: 'u-42',
        status: 'ACTIVE',
      );
}

/// Router de prueba con las 3 paginas compartiendo [controller].
GoRouter _testRouter(
  KycFlowController controller, {
  SessionIdentityStore? identity,
}) =>
    GoRouter(
      initialLocation: '/kyc',
      routes: [
        GoRoute(
          path: '/kyc',
          builder: (context, state) => KycStartPage(controller: controller),
        ),
        GoRoute(
          path: '/kyc/document',
          builder: (context, state) => KycDocumentPage(controller: controller),
        ),
        GoRoute(
          path: '/kyc/task',
          builder: (context, state) => KycTaskPage(controller: controller),
        ),
        GoRoute(
          path: '/kyc/result',
          builder: (context, state) => KycResultPage(
            controller: controller,
            identity: identity,
          ),
        ),
        GoRoute(
          path: '/pin-setup',
          builder: (context, state) =>
              const Scaffold(body: Text('pin-setup-ok')),
        ),
      ],
    );

Future<void> _pumpKyc(
  WidgetTester tester,
  KycFlowController controller, {
  SessionIdentityStore? identity,
}) async {
  await tester.pumpWidget(
    MaterialApp.router(routerConfig: _testRouter(controller, identity: identity)),
  );
  await tester.pumpAndSettle();
}

Future<void> _tapContinue(WidgetTester tester) async {
  await tester.ensureVisible(find.text('Continuar'));
  await tester.tap(find.text('Continuar'));
  await tester.pumpAndSettle();
}

Future<void> _completeStartPage(WidgetTester tester) async {
  await tester.enterText(find.byKey(const Key('firstNameField')), 'Ana');
  await tester.enterText(find.byKey(const Key('lastNameField')), 'Perez');
  await tester.enterText(
    find.byKey(const Key('emailField')),
    'ana@example.com',
  );
  await tester.enterText(
    find.byKey(const Key('docNumberField')),
    '12345678',
  );
  await _tapContinue(tester);
}

/// F-T23: tras los datos, el flujo pasa por `/kyc/document`; captura la foto
/// (mock en tests) y continua a las tareas de liveness.
Future<void> _captureDocument(WidgetTester tester) async {
  expect(find.text('Fotografia tu documento'), findsOneWidget);
  await tester.tap(find.byKey(const Key('captureDocumentButton')));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('start muestra la version visible del build (F-T30)',
      (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    expect(find.byType(AppVersionLabel), findsOneWidget);
    expect(find.byKey(const Key('app-version')), findsOneWidget);
    expect(find.text('Version $kAppVersion'), findsOneWidget);
  });

  testWidgets('start muestra los campos de titular y documento',
      (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    expect(find.byKey(const Key('firstNameField')), findsOneWidget);
    expect(find.byKey(const Key('lastNameField')), findsOneWidget);
    expect(find.byKey(const Key('emailField')), findsOneWidget);
    expect(find.byKey(const Key('phoneField')), findsOneWidget);
    expect(find.byKey(const Key('docTypeField')), findsOneWidget);
    expect(find.byKey(const Key('docNumberField')), findsOneWidget);
  });

  testWidgets('start valida numero vacio y no pide desafio', (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _tapContinue(tester);

    expect(find.text('Ingresa el numero de documento'), findsOneWidget);
    expect(controller.challenge, isNull);
  });

  testWidgets('email obligatorio con formato valido (no pide desafio)',
      (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await tester.enterText(find.byKey(const Key('firstNameField')), 'Ana');
    await tester.enterText(find.byKey(const Key('lastNameField')), 'Perez');
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '12345678',
    );
    await _tapContinue(tester);
    expect(find.text('Ingresa tu email'), findsOneWidget);
    expect(controller.challenge, isNull);

    await tester.enterText(
      find.byKey(const Key('emailField')),
      'ana-sin-arroba',
    );
    await _tapContinue(tester);
    expect(find.text('Ingresa un email valido'), findsOneWidget);
    expect(controller.challenge, isNull);
  });

  testWidgets('flujo completo happy path hasta resultado exitoso',
      (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await _captureDocument(tester);
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);

    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Paso 2 de 2'), findsOneWidget);

    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    expect(find.text('Enviar verificacion'), findsOneWidget);

    await tester.tap(find.text('Enviar verificacion'));
    await tester.pumpAndSettle();
    expect(find.text('Verificacion exitosa'), findsOneWidget);
  });

  testWidgets('exito del KYC guarda user_ref y navega a /pin-setup',
      (tester) async {
    final identity = InMemorySessionIdentityStore();
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller, identity: identity);

    await _completeStartPage(tester);
    await _captureDocument(tester);
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Enviar verificacion'));
    await tester.pumpAndSettle();
    expect(find.text('Verificacion exitosa'), findsOneWidget);

    await tester.tap(find.byKey(const Key('kyc-result-continue')));
    await tester.pumpAndSettle();

    expect(identity.userRef, 'u-42');
    expect(find.text('pin-setup-ok'), findsOneWidget);
  });

  testWidgets('fallo al guardar user_ref no bloquea el alta', (tester) async {
    final identity = _ThrowingIdentityStore();
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller, identity: identity);

    await _completeStartPage(tester);
    await _captureDocument(tester);
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Enviar verificacion'));
    await tester.pumpAndSettle();
    expect(find.text('Verificacion exitosa'), findsOneWidget);

    await tester.tap(find.byKey(const Key('kyc-result-continue')));
    await tester.pumpAndSettle();

    // Best-effort: aunque falle el store, el alta continua a /pin-setup.
    expect(find.text('pin-setup-ok'), findsOneWidget);
    expect(find.textContaining('No pudimos guardar'), findsOneWidget);

    // Drenar el temporizador del SnackBar para no dejar timers pendientes.
    await tester.pump(const Duration(seconds: 5));
    await tester.pumpAndSettle();
  });

  testWidgets('reintento tras passed:false conserva la misma tarea',
      (tester) async {
    var calls = 0;
    final controller = KycFlowController(
      service: const FakeKycService(),
      taskEvaluator: (task, frames) async => ++calls >= 2,
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await _captureDocument(tester);

    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    // Sigue en la misma tarea con mensaje de reintento.
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
    expect(find.textContaining('misma tarea'), findsOneWidget);

    await tester.tap(find.text('Reintentar captura'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Paso 2 de 2'), findsOneWidget);
  });

  testWidgets('resultado fallido muestra el motivo del servidor',
      (tester) async {
    final controller = KycFlowController(service: _FailingSubmitService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await _captureDocument(tester);
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Enviar verificacion'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Motivo: LOW_QUALITY'), findsOneWidget);
    expect(find.text('Reintentar verificacion'), findsOneWidget);
  });

  testWidgets(
      'navegacion F-T23: /kyc -> /kyc/document -> /kyc/task -> /kyc/result',
      (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    // Intermedia obligatoria de documento (camara trasera).
    expect(find.text('Fotografia tu documento'), findsOneWidget);
    expect(controller.hasDocumentImage, isFalse);

    await tester.tap(find.byKey(const Key('captureDocumentButton')));
    await tester.pumpAndSettle();
    expect(controller.hasDocumentImage, isTrue);
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);

    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Enviar verificacion'));
    await tester.pumpAndSettle();
    expect(find.text('Verificacion exitosa'), findsOneWidget);
  });

  testWidgets('evaluador no-pasado muestra el paso fallido y el motivo',
      (tester) async {
    var calls = 0;
    final controller = KycFlowController(
      service: const FakeKycService(),
      detailedTaskEvaluator: (task, frames) async {
        calls++;
        return calls == 1
            ? const KycTaskEvaluation(
                step: 'front',
                passed: false,
                reason: 'NO_BLINK',
              )
            : const KycTaskEvaluation(step: 'front', passed: true);
      },
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await _captureDocument(tester);
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();

    // No avanzo: sigue en la misma tarea y muestra paso + motivo traducido.
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
    expect(find.byKey(const Key('kycFailedStep')), findsOneWidget);
    expect(
      tester.widget<Text>(find.byKey(const Key('kycFailedStep'))).data,
      contains('front'),
    );
    expect(
      tester.widget<Text>(find.byKey(const Key('kycReasonMessage'))).data,
      contains('parpadeo'),
    );

    // Reintento de la MISMA tarea: ahora pasa y avanza.
    await tester.tap(find.text('Reintentar captura'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Paso 2 de 2'), findsOneWidget);
  });

  testWidgets('resultado fallido muestra el paso y el motivo traducido',
      (tester) async {
    final controller = KycFlowController(service: _FailedStepSubmitService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await _captureDocument(tester);
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Capturar'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Enviar verificacion'));
    await tester.pumpAndSettle();

    expect(
      tester.widget<Text>(find.byKey(const Key('kycResultFailedStep'))).data,
      contains('blink'),
    );
    expect(
      tester.widget<Text>(find.byKey(const Key('kycResultReason'))).data,
      contains('movimiento'),
    );
  });

  testWidgets('F-T26: documento valido avanza al challenge', (tester) async {
    final controller = KycFlowController(
      service: const FakeKycService(),
      documentValidator: (image) async =>
          const KycDocumentValidation(isValid: true),
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await _captureDocument(tester);

    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
    expect(controller.hasDocumentImage, isTrue);
  });

  testWidgets('F-T26: documento invalido muestra issues y no avanza',
      (tester) async {
    var calls = 0;
    final controller = KycFlowController(
      service: const FakeKycService(),
      documentValidator: (image) async {
        calls++;
        return calls == 1
            ? const KycDocumentValidation(isValid: false, issues: ['BLURRY'])
            : const KycDocumentValidation(isValid: true);
      },
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await tester.tap(find.byKey(const Key('captureDocumentButton')));
    await tester.pumpAndSettle();

    // No avanzo: sigue en documento, con los motivos en espanol.
    expect(find.text('Fotografia tu documento'), findsOneWidget);
    expect(find.textContaining('Paso 1 de 2'), findsNothing);
    expect(find.byKey(const Key('kycDocumentIssues')), findsOneWidget);
    expect(find.textContaining('borrosa'), findsOneWidget);
    expect(controller.hasDocumentImage, isTrue);

    // Reintento de captura: "Volver a capturar" resetea y re-monta el preview
    // (F-T27); una nueva captura valida de nuevo y ahora si avanza.
    await tester.ensureVisible(find.byKey(const Key('recaptureDocumentButton')));
    await tester.tap(find.byKey(const Key('recaptureDocumentButton')));
    await tester.pumpAndSettle();
    expect(controller.hasDocumentImage, isFalse);
    await tester.ensureVisible(find.byKey(const Key('captureDocumentButton')));
    await tester.tap(find.byKey(const Key('captureDocumentButton')));
    await tester.pumpAndSettle();
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
    expect(calls, 2);
  });

  testWidgets('F-T26: error de red al validar conserva la captura y reintenta',
      (tester) async {
    var calls = 0;
    final controller = KycFlowController(
      service: const FakeKycService(),
      documentValidator: (image) async {
        calls++;
        if (calls == 1) throw ApiException.network();
        return const KycDocumentValidation(isValid: true);
      },
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await tester.tap(find.byKey(const Key('captureDocumentButton')));
    await tester.pumpAndSettle();

    // Se muestra el error y NO se avanza; la captura sigue en memoria.
    expect(find.textContaining('Sin conexion'), findsOneWidget);
    expect(find.textContaining('Paso 1 de 2'), findsNothing);
    expect(controller.hasDocumentImage, isTrue);
    final image = controller.documentImage;

    // Reintenta la validacion sin recapturar: conserva la misma imagen.
    await tester.ensureVisible(find.text('Reintentar validacion'));
    await tester.tap(find.text('Reintentar validacion'));
    await tester.pumpAndSettle();
    expect(controller.documentImage, image);
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
  });
}

/// Store que falla al guardar el `user_ref` (secure storage caido).
class _ThrowingIdentityStore implements SessionIdentityStore {
  @override
  String? get userRef => null;

  @override
  String? get deviceId => null;

  @override
  Future<void> load() async {}

  @override
  Future<void> saveUserRef(String userRef) async =>
      throw StateError('secure storage caido');

  @override
  Future<String?> readUserRef() async => null;

  @override
  Future<String> getOrCreateDeviceId() async => 'd-1';

  @override
  Future<String?> readDeviceId() async => null;
}

class _FailingSubmitService implements KycService {
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
      const KycSubmitResult(overallResult: false, detailCode: 'LOW_QUALITY');
}

/// Submit fallido con `failed_step`/`overall_reason` (E1-T29) para verificar
/// que la UI muestra QUE paso fallo y el motivo traducido (F-T23).
class _FailedStepSubmitService implements KycService {
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
      const KycSubmitResult(
        overallResult: false,
        detailCode: 'LIVENESS_FAILED',
        failedStep: 'blink',
        overallReason: 'INSUFFICIENT_MOVEMENT',
      );
}
