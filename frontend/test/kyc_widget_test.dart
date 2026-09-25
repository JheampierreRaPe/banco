import 'dart:typed_data';

import 'package:banca_online/core/app_version.dart';
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/core/widgets/app_button.dart';
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

/// Doble del lookup de titular (F-T44, E1-T35): devuelve Ana Perez por
/// defecto para conservar el flujo happy path (los nombres solo los llena la
/// API; los campos son `readOnly`).
class FakeKycDocumentLookup implements KycDocumentLookupService {
  const FakeKycDocumentLookup({
    this.owner = const KycDocumentOwner(
      documentType: 'DNI',
      firstName: 'Ana',
      lastName: 'Perez',
    ),
  });

  final KycDocumentOwner owner;

  @override
  Future<KycDocumentOwner> lookupDocument({
    required String type,
    required String number,
  }) async =>
      owner;
}

/// Router de prueba con las 3 paginas compartiendo [controller].
GoRouter _testRouter(
  KycFlowController controller, {
  SessionIdentityStore? identity,
  KycDocumentLookupService lookupService = const FakeKycDocumentLookup(),
}) =>
    GoRouter(
      initialLocation: '/kyc',
      routes: [
        GoRoute(
          path: '/kyc',
          builder: (context, state) => KycStartPage(
            controller: controller,
            lookupService: lookupService,
          ),
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
  KycDocumentLookupService lookupService = const FakeKycDocumentLookup(),
}) async {
  await tester.pumpWidget(
    MaterialApp.router(
        routerConfig:
            _testRouter(controller, identity: identity, lookupService: lookupService)),
  );
  await tester.pumpAndSettle();
}

Future<void> _tapContinue(WidgetTester tester) async {
  await tester.ensureVisible(find.text('Continuar'));
  await tester.tap(find.text('Continuar'));
  await tester.pumpAndSettle();
}

/// Toca un botón de la página de tareas (hace scroll previo: la página
/// rediseñada F-T38 es más alta que el viewport de pruebas).
Future<void> _tapText(WidgetTester tester, String label) async {
  await tester.ensureVisible(find.text(label));
  await tester.tap(find.text(label));
  await tester.pumpAndSettle();
}

/// Completa la pantalla de inicio F-T44: email + numero, `Validar documento`
/// (los nombres los rellena el lookup mock; son `readOnly`) y `Continuar`.
Future<void> _completeStartPage(WidgetTester tester) async {
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
  await _tapContinue(tester);
}

/// `Continuar` habilitado (`null` = bloqueado, F-T44 exige documento
/// validado por el servidor).
bool _continuarEnabled(WidgetTester tester) =>
    tester
        .widget<AppPrimaryButton>(
          find.widgetWithText(AppPrimaryButton, 'Continuar'),
        )
        .onPressed !=
    null;

/// F-T23/F-T45: tras los datos, el flujo pasa por `/kyc/document`; captura
/// la foto (mock en tests, solo estado Capturado) y pulsa `Continuar` para
/// validar contra el servidor y avanzar a liveness solo con `is_valid: true`.
Future<void> _captureDocument(WidgetTester tester) async {
  expect(find.text('Escanea tu DNI'), findsOneWidget);
  await tester.tap(find.byKey(const Key('captureDocumentButton')));
  await tester.pumpAndSettle();
  // F-T45: la captura sola no navega; Continuar dispara la validacion.
  await tester.ensureVisible(find.byKey(const Key('kycDocumentContinue')));
  await tester.tap(find.byKey(const Key('kycDocumentContinue')));
  await tester.pumpAndSettle();
}

/// Captura SIN validar (deja el estado Capturado, F-T45).
Future<void> _captureOnly(WidgetTester tester) async {
  expect(find.text('Escanea tu DNI'), findsOneWidget);
  await tester.tap(find.byKey(const Key('captureDocumentButton')));
  await tester.pumpAndSettle();
}

/// Pulsa Continuar del paso de documento (dispara la validacion, F-T45).
Future<void> _continueDocument(WidgetTester tester) async {
  await tester.ensureVisible(find.byKey(const Key('kycDocumentContinue')));
  await tester.tap(find.byKey(const Key('kycDocumentContinue')));
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

  testWidgets('start exige documento validado: Continuar bloqueado en vacio',
      (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    // F-T44: sin `Validar documento` no se puede continuar ni pedir desafio.
    expect(_continuarEnabled(tester), isFalse);
    expect(controller.challenge, isNull);
  });

  testWidgets('email obligatorio con formato valido (no pide desafio)',
      (tester) async {
    final controller = KycFlowController(service: const FakeKycService());
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    // Valida el documento primero (nombres desde la API); sin email,
    // Continuar sigue bloqueado.
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '12345678',
    );
    await tester.pump();
    await tester.ensureVisible(find.byKey(const Key('validateDocumentButton')));
    await tester.tap(find.byKey(const Key('validateDocumentButton')));
    await tester.pumpAndSettle();
    expect(_continuarEnabled(tester), isFalse);
    expect(controller.challenge, isNull);

    await tester.enterText(
      find.byKey(const Key('emailField')),
      'ana-sin-arroba',
    );
    await tester.pump();
    expect(_continuarEnabled(tester), isFalse);
    expect(controller.challenge, isNull);

    await tester.enterText(
      find.byKey(const Key('emailField')),
      'ana@example.com',
    );
    await tester.pump();
    expect(_continuarEnabled(tester), isTrue);
    expect(controller.challenge, isNull);
  });

  testWidgets('flujo completo happy path hasta resultado exitoso',
      (tester) async {
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

    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    expect(find.textContaining('Paso 2 de 2'), findsOneWidget);

    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    expect(find.text('Enviar verificación'), findsOneWidget);

    await _tapText(tester, 'Enviar verificación');
    await tester.pumpAndSettle();
    expect(find.text('Verificacion exitosa'), findsOneWidget);
  });

  testWidgets('exito del KYC navega a /pin-setup SIN guardar user_ref',
      (tester) async {
    // Guardado tardio (F-T39/SCR-005): el resultado ya NO persiste el
    // `user_ref`; solo el paso success lo guarda.
    final identity = InMemorySessionIdentityStore();
    final controller = KycFlowController(
      service: const FakeKycService(),
      documentValidator: (image) async =>
          const KycDocumentValidation(isValid: true),
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller, identity: identity);

    await _completeStartPage(tester);
    await _captureDocument(tester);
    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    await _tapText(tester, 'Enviar verificación');
    await tester.pumpAndSettle();
    expect(find.text('Verificacion exitosa'), findsOneWidget);

    await tester.tap(find.byKey(const Key('kyc-result-continue')));
    await tester.pumpAndSettle();

    expect(identity.userRef, isNull);
    expect(find.text('pin-setup-ok'), findsOneWidget);
  });

  testWidgets('sin user_id no navega y avisa (no se inventa la referencia)',
      (tester) async {
    final controller = KycFlowController(
      service: _NoUserIdSubmitService(),
      documentValidator: (image) async =>
          const KycDocumentValidation(isValid: true),
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await _captureDocument(tester);
    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    await _tapText(tester, 'Enviar verificación');
    await tester.pumpAndSettle();
    expect(find.text('Verificacion exitosa'), findsOneWidget);

    await tester.tap(find.byKey(const Key('kyc-result-continue')));
    await tester.pumpAndSettle();

    // No navega: sigue en el resultado con el aviso.
    expect(find.text('Verificacion exitosa'), findsOneWidget);
    expect(find.text('pin-setup-ok'), findsNothing);
    expect(
      find.text('No pudimos obtener tu identificador. Vuelve a intentarlo.'),
      findsOneWidget,
    );

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
      documentValidator: (image) async =>
          const KycDocumentValidation(isValid: true),
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await _captureDocument(tester);

    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    // Sigue en la misma tarea con mensaje de reintento.
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
    expect(find.textContaining('misma tarea'), findsOneWidget);

    await _tapText(tester, 'Reintentar captura');
    await tester.pumpAndSettle();
    expect(find.textContaining('Paso 2 de 2'), findsOneWidget);
  });

  testWidgets(
      'F-T47: submit fallido reinicia todo en silencio a /kyc (sin popup)',
      (tester) async {
    final controller = KycFlowController(
      service: _FailingSubmitService(),
      documentValidator: (image) async =>
          const KycDocumentValidation(isValid: true),
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    await _captureDocument(tester);
    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    await _tapText(tester, 'Enviar verificación');
    await tester.pumpAndSettle();

    // F-T47 (decisión del dueño): el fallo final ya no muestra el flujo de
    // reintento con el estado anterior; vuelve a `/kyc` en silencio.
    expect(find.byKey(const Key('kycBackConfirmDialog')), findsNothing);
    expect(find.text('Empecemos por ti'), findsOneWidget);
    expect(controller.challenge, isNull);
    expect(controller.result, isNull);
    expect(controller.documentType, 'DNI');
    expect(controller.documentNumber, isEmpty);
    expect(controller.applicant, isNull);
  });

  testWidgets(
      'navegacion F-T23: /kyc -> /kyc/document -> /kyc/task -> /kyc/result',
      (tester) async {
    final controller = KycFlowController(
      service: const FakeKycService(),
      documentValidator: (image) async =>
          const KycDocumentValidation(isValid: true),
    );
    addTearDown(controller.dispose);
    await _pumpKyc(tester, controller);

    await _completeStartPage(tester);
    // Intermedia obligatoria de documento (camara trasera).
    expect(find.text('Escanea tu DNI'), findsOneWidget);
    expect(controller.hasDocumentImage, isFalse);

    // F-T45: captura + Continuar valida y avanza solo con is_valid:true.
    await tester.tap(find.byKey(const Key('captureDocumentButton')));
    await tester.pumpAndSettle();
    expect(controller.hasDocumentImage, isTrue);
    await _continueDocument(tester);
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);

    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    await _tapText(tester, 'Capturar');
    await tester.pumpAndSettle();
    await _tapText(tester, 'Enviar verificación');
    await tester.pumpAndSettle();
    expect(find.text('Verificacion exitosa'), findsOneWidget);
  });

  testWidgets('evaluador no-pasado muestra el paso fallido y el motivo',
      (tester) async {
    var calls = 0;
    final controller = KycFlowController(
      service: const FakeKycService(),
      documentValidator: (image) async =>
          const KycDocumentValidation(isValid: true),
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
    await _tapText(tester, 'Capturar');
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
    await _tapText(tester, 'Reintentar captura');
    await tester.pumpAndSettle();
    expect(find.textContaining('Paso 2 de 2'), findsOneWidget);
  });

  testWidgets(
      'F-T47: reintentar en el resultado reinicia todo y vuelve a /kyc',
      (tester) async {
    // Cobertura del fallback de `/kyc/result` con fallo: muestra QUÉ paso
    // falló y el motivo traducido, y `Reintentar verificacion` hace
    // `resetFull()` + `go('/kyc')` (ya no reintenta con el estado anterior).
    final controller = KycFlowController(
      service: _FailedStepSubmitService(),
      documentValidator: (image) async =>
          const KycDocumentValidation(isValid: true),
    );
    addTearDown(controller.dispose);
    await controller.loadChallenge();
    controller.setDocument(type: 'DNI', number: '12345678');
    controller.setApplicant(const KycApplicant(
      firstName: 'Ana',
      lastName: 'Perez',
      email: 'ana@example.com',
    ));
    await controller.captureAndResolveCurrentTask();
    await controller.captureAndResolveCurrentTask();
    await controller.submit();
    expect(controller.result?.overallResult, isFalse);

    final router = GoRouter(
      initialLocation: '/kyc/result',
      routes: [
        GoRoute(
          path: '/kyc',
          builder: (context, state) => KycStartPage(
            controller: controller,
            lookupService: const FakeKycDocumentLookup(),
          ),
        ),
        GoRoute(
          path: '/kyc/result',
          builder: (context, state) =>
              KycResultPage(controller: controller),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(
      MaterialApp.router(routerConfig: router),
    );
    await tester.pumpAndSettle();

    expect(
      tester.widget<Text>(find.byKey(const Key('kycResultFailedStep'))).data,
      contains('blink'),
    );
    expect(
      tester.widget<Text>(find.byKey(const Key('kycResultReason'))).data,
      contains('movimiento'),
    );

    await _tapText(tester, 'Reintentar verificacion');
    expect(find.text('Empecemos por ti'), findsOneWidget);
    expect(controller.challenge, isNull);
    expect(controller.result, isNull);
    expect(controller.documentType, 'DNI');
    expect(controller.documentNumber, isEmpty);
    expect(controller.applicant, isNull);
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
    // F-T45: captura deja Capturado; Continuar dispara la validacion.
    await _captureOnly(tester);
    expect(find.text('Foto capturada'), findsOneWidget);
    await _continueDocument(tester);

    // No avanzo: sigue en documento, con los motivos en espanol.
    expect(find.text('Escanea tu DNI'), findsOneWidget);
    expect(find.textContaining('Paso 1 de 2'), findsNothing);
    expect(find.byKey(const Key('kycDocumentIssues')), findsOneWidget);
    expect(find.textContaining('borrosa'), findsOneWidget);
    expect(controller.hasDocumentImage, isTrue);

    // Reintento de captura: "Volver a tomar" resetea y re-monta el preview
    // (F-T27); una nueva captura + Continuar valida de nuevo y ahora avanza.
    await tester.ensureVisible(find.byKey(const Key('recaptureDocumentButton')));
    await tester.tap(find.byKey(const Key('recaptureDocumentButton')));
    await tester.pumpAndSettle();
    expect(controller.hasDocumentImage, isFalse);
    await tester.ensureVisible(find.byKey(const Key('captureDocumentButton')));
    await tester.tap(find.byKey(const Key('captureDocumentButton')));
    await tester.pumpAndSettle();
    await _continueDocument(tester);
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
    // F-T45: captura deja Capturado; Continuar dispara la validacion.
    await _captureOnly(tester);
    await _continueDocument(tester);

    // Se muestra el error y NO se avanza; la captura sigue en memoria.
    expect(find.textContaining('Sin conexi'), findsOneWidget);
    expect(find.textContaining('Paso 1 de 2'), findsNothing);
    expect(controller.hasDocumentImage, isTrue);
    final image = controller.documentImage;

    // Reintenta la validacion sin recapturar: conserva la misma imagen.
    await tester.ensureVisible(find.text('Reintentar validación'));
    await tester.tap(find.text('Reintentar validación'));
    await tester.pumpAndSettle();
    expect(controller.documentImage, image);
    expect(find.textContaining('Paso 1 de 2'), findsOneWidget);
  });
}

/// Submit exitoso SIN `user_id` (backend legacy): no se inventa referencia.
class _NoUserIdSubmitService implements KycService {
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
