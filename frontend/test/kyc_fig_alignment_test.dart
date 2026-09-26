import 'dart:typed_data';

import 'package:banca_online/core/theme/app_colors.dart';
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

/// Alineación del flujo KYC al diseño canónico (F-T38).
///
/// Verifica los textos del `.fig` (datos `0:789`, captura `0:354`, DNI no
/// legible `0:121`, facial `0:198` en claro, error facial `0:269`, errores de
/// registro `0:407`), los tokens de F-T34 y los estados obligatorios
/// (docs/20 §7). Sin lógica de negocio: la app solo captura y muestra.
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

/// Doble del lookup de titular (F-T44/E1-T35): sin red, sin PII real.
/// Devuelve el titular canónico para DNI válido; la UI lo vuelca en
/// `Nombres`/`Apellidos` (`readOnly`) y habilita `Continuar`.
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

GoRouter _router(
  KycFlowController controller, {
  KycDocumentLookupService lookupService = const _FakeDocumentLookup(),
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
          builder: (context, state) => KycTaskPage(
            controller: controller,
            prepDuration: Duration.zero,
          ),
        ),
        GoRoute(
          path: '/kyc/result',
          builder: (context, state) => KycResultPage(controller: controller),
        ),
        GoRoute(
          path: '/pin-setup',
          builder: (context, state) =>
              const Scaffold(body: Text('pin-setup-ok')),
        ),
      ],
    );

Future<void> _pumpStart(
  WidgetTester tester,
  KycFlowController c, {
  KycDocumentLookupService lookupService = const _FakeDocumentLookup(),
}) async {
  await tester.pumpWidget(
    MaterialApp.router(
      routerConfig: _router(c, lookupService: lookupService),
    ),
  );
  await tester.pumpAndSettle();
}

/// Flujo documentado F-T44: número + correo y `Validar documento` (el
/// lookup rellena `Nombres`/`Apellidos` `readOnly`). Sin PII real.
Future<void> _fillStartForm(WidgetTester tester) async {
  await tester.enterText(
    find.byKey(const Key('docNumberField')),
    '12345678',
  );
  await tester.enterText(
    find.byKey(const Key('emailField')),
    'ana@example.com',
  );
  await tester.pump();
}

Future<void> _validarDocumento(WidgetTester tester) async {
  await tester.ensureVisible(find.byKey(const Key('validateDocumentButton')));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const Key('validateDocumentButton')));
  await tester.pumpAndSettle();
}

/// `fill + Validar + Continuar`: la ruta documentada hasta `/kyc/document`.
Future<void> _fillValidateAndContinue(WidgetTester tester) async {
  await _fillStartForm(tester);
  await _validarDocumento(tester);
  await _tapContinuar(tester);
}

Future<void> _tapContinuar(WidgetTester tester) async {
  await tester.ensureVisible(find.text('Continuar'));
  await tester.tap(find.text('Continuar'));
  await tester.pumpAndSettle();
}

/// Toca un botón de la página de tareas con scroll previo (página F-T38 más
/// alta que el viewport de pruebas).
Future<void> _tapTaskButton(WidgetTester tester, String label) async {
  await tester.ensureVisible(find.text(label));
  await tester.tap(find.text(label));
  await tester.pumpAndSettle();
}

void main() {
  group('F-T38 datos personales (fig 0:789 + errores 0:407)', () {
    testWidgets('muestra el diseño canónico con tokens claros', (tester) async {
      final c = KycFlowController(service: const _FakeKycService());
      addTearDown(c.dispose);
      await _pumpStart(tester, c);

      expect(find.text('Crear cuenta'), findsOneWidget);
      expect(find.text('Paso 1 de 4 · Datos'), findsOneWidget);
      expect(find.text('Empecemos por ti'), findsOneWidget);
      // F-T44 (`pantalla crearCuenta-1Datos` `0:1201`, `Sub` `0:1272`):
      // el subtítulo canónico dice "documento", no "DNI".
      expect(
        find.text('Ingresa tus datos tal como figuran en tu documento.'),
        findsOneWidget,
      );
      expect(
        find.text('Ingresa tus datos tal como figuran en tu DNI.'),
        findsNothing,
      );
      expect(find.text('Tipo de documento'), findsOneWidget);
      expect(find.text('Opciones: DNI · RUC'), findsOneWidget);
      expect(find.text('Número de documento'), findsOneWidget);
      expect(find.text('Validar documento'), findsOneWidget);
      expect(find.text('Nombres'), findsOneWidget);
      expect(find.text('Apellidos'), findsOneWidget);
      expect(find.text('Correo electrónico'), findsOneWidget);
      expect(find.text('Teléfono (opcional)'), findsOneWidget);
      expect(find.text('Solo números · 8 dígitos'), findsOneWidget);
      expect(find.text('12345678'), findsOneWidget);
      expect(
        find.textContaining('Aquí te enviaremos tus constancias'),
        findsOneWidget,
      );
      expect(find.textContaining('Validaremos tu identidad'), findsOneWidget);
      expect(
        find.text(
          'Validaremos tu identidad con una foto de tu documento y '
          'reconocimiento facial.',
        ),
        findsOneWidget,
      );
      // Nombres/Apellidos solo los llena la API (F-T44): no editables.
      final firstInner = find.descendant(
        of: find.byKey(const Key('firstNameField')),
        matching: find.byType(TextField),
      );
      expect(
        tester.widget<TextField>(firstInner).readOnly,
        isTrue,
      );
      final lastInner = find.descendant(
        of: find.byKey(const Key('lastNameField')),
        matching: find.byType(TextField),
      );
      expect(
        tester.widget<TextField>(lastInner).readOnly,
        isTrue,
      );
      // `Continuar` nace bloqueado hasta validar el documento (F-T44).
      final continuar = tester.widget<AppPrimaryButton>(
        find.widgetWithText(AppPrimaryButton, 'Continuar'),
      );
      expect(continuar.onPressed, isNull);
      expect(find.text('Continuar'), findsOneWidget);
      expect(find.byType(AppVersionLabel), findsOneWidget);

      // Solo modo claro (docs/20): fondo `surface`, sin superficies oscuras.
      final scaffold = tester.widget<Scaffold>(find.byType(Scaffold).first);
      expect(scaffold.backgroundColor, AppColors.surface);
    });

    testWidgets('formulario inválido muestra el banner de errores',
        (tester) async {
      final c = KycFlowController(service: const _FakeKycService());
      addTearDown(c.dispose);
      await _pumpStart(tester, c);

      // F-T44: con el formulario vacío `Continuar` nace DESHABILITADO
      // (gate de documento validado) y el banner no existe todavía.
      final continuar = tester.widget<AppPrimaryButton>(
        find.widgetWithText(AppPrimaryButton, 'Continuar'),
      );
      expect(continuar.onPressed, isNull);
      expect(find.byKey(const Key('kycFormErrorBanner')), findsNothing);

      // Intentar avanzar sin validar no navega ni pide challenge: el gate
      // bloquea más fuerte que el banner antiguo (fail-closed de F-T44).
      await tester.tap(find.text('Continuar'), warnIfMissed: false);
      await tester.pumpAndSettle();

      expect(find.text('Crear cuenta'), findsOneWidget);
      expect(find.text('Escanea tu DNI'), findsNothing);
      expect(c.challenge, isNull);
      expect(c.documentNumber, isEmpty);
      expect(find.byKey(const Key('kycFormErrorBanner')), findsNothing);
    });

    testWidgets('cargando y error del challenge conservan el estilo',
        (tester) async {
      final c = KycFlowController(service: const _FakeKycService());
      addTearDown(c.dispose);
      await _pumpStart(tester, c);

      // F-T44: la navegación exige documento validado + correo válido; se
      // conduce por la ruta documentada (lookup Fake -> Validar -> Continuar).
      await _fillValidateAndContinue(tester);

      // Avanzó al documento: el challenge se pidió y el estilo se conserva.
      expect(find.text('Escanea tu DNI'), findsOneWidget);
      expect(c.challenge, isNotNull);
      expect(c.challenge?.token, 'tok-abc');
      expect(c.documentNumber, '12345678');
      expect(c.applicant?.email, 'ana@example.com');
    });
  });

  group('F-T38 captura DNI (fig 0:354) y no legible (fig 0:121)', () {
    testWidgets('muestra la tarjeta de captura canónica', (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: true),
      );
      addTearDown(c.dispose);
      await _pumpStart(tester, c);

      // F-T44+F-T45: se llega vía lookup válido (gate de "Continuar").
      await _fillValidateAndContinue(tester);

      expect(find.text('Verifica tu identidad'), findsOneWidget);
      expect(find.text('Paso 2 de 4 · Documento'), findsOneWidget);
      expect(find.text('Escanea tu DNI'), findsOneWidget);
      expect(
        find.textContaining('superficie plana'),
        findsOneWidget,
      );
      expect(find.text('Frente del DNI'), findsOneWidget);
      expect(find.text('Foto y datos personales'), findsOneWidget);
      expect(find.text('Tomar foto'), findsOneWidget);
      expect(find.textContaining('se cifran'), findsOneWidget);
    });

    testWidgets('documento no legible muestra badge, motivo y reintento',
        (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        documentValidator: (image) async => const KycDocumentValidation(
          isValid: false,
          issues: ['BLURRY'],
        ),
      );
      addTearDown(c.dispose);
      await _pumpStart(tester, c);

      // F-T44+F-T45: se llega vía lookup válido (gate de "Continuar").
      await _fillValidateAndContinue(tester);
      await tester.tap(find.byKey(const Key('captureDocumentButton')));
      await tester.pumpAndSettle();
      // F-T45: captura deja Capturado; Continuar valida y deja No legible.
      await tester.ensureVisible(find.byKey(const Key('kycDocumentContinue')));
      await tester.tap(find.byKey(const Key('kycDocumentContinue')));
      await tester.pumpAndSettle();

      expect(find.text('No pudimos leer tu DNI'), findsOneWidget);
      expect(find.text('No legible'), findsOneWidget);
      expect(find.text('Foto con problemas de lectura'), findsOneWidget);
      expect(find.byKey(const Key('kycDocumentIssues')), findsOneWidget);
      expect(find.textContaining('borrosa'), findsOneWidget);
      expect(find.text('Volver a tomar'), findsOneWidget);
      // El fig deshabilita Continuar hasta lograr una captura válida.
      final continuar = tester.widget<AppPrimaryButton>(
        find.widgetWithText(AppPrimaryButton, 'Continuar'),
      );
      expect(continuar.onPressed, isNull);
    });
  });

  group('F-T38 facial en claro (fig 0:198) y error (fig 0:269)', () {
    testWidgets('muestra el viewport circular en modo claro', (tester) async {
      final c = KycFlowController(service: const _FakeKycService());
      addTearDown(c.dispose);
      await c.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: c, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('Reconocimiento facial'), findsOneWidget);
      expect(find.text('Paso 1 de 2 · Rostro'), findsOneWidget);
      expect(
        find.text('Centra tu rostro en el círculo'),
        findsOneWidget,
      );
      expect(find.textContaining('Mira de frente'), findsOneWidget);
      // F-T48: el checklist se retiró por decisión del dueño (ni card ni
      // espacio en kyc-task).
      expect(find.text('Buena iluminación'), findsNothing);
      expect(find.text('Rostro descubierto'), findsNothing);
      expect(find.textContaining('Prueba de vida'), findsNothing);
      expect(
        find.text('No cierres la app durante la verificación.'),
        findsOneWidget,
      );

      // Desviación documentada del fig: el mockup es dark, la app es clara.
      final scaffold = tester.widget<Scaffold>(find.byType(Scaffold));
      expect(scaffold.backgroundColor, AppColors.surface);
    });

    testWidgets('fallo facial muestra paso, motivo e intento', (tester) async {
      final c = KycFlowController(
        service: const _FakeKycService(),
        detailedTaskEvaluator: (task, frames) async =>
            const KycTaskEvaluation(step: 'front', passed: false, reason: 'NO_BLINK'),
      );
      addTearDown(c.dispose);
      await c.loadChallenge();

      await tester.pumpWidget(
        MaterialApp(
          home: KycTaskPage(controller: c, prepDuration: Duration.zero),
        ),
      );
      await tester.pumpAndSettle();

      await _tapTaskButton(tester, 'Capturar');
      await tester.pumpAndSettle();

      expect(find.text('No pudimos verificarte'), findsOneWidget);
      expect(
        find.textContaining('bien iluminado'),
        findsOneWidget,
      );
      expect(find.byKey(const Key('kycFailedStep')), findsOneWidget);
      expect(find.byKey(const Key('kycReasonMessage')), findsOneWidget);
      expect(find.text('Intento 1 de 3'), findsOneWidget);
    });
  });

  group('F-T38 resultado conserva failed_step y estilos', () {
    testWidgets(
        'F-T47: fallo final reinicia todo en silencio a /kyc (sin popup)',
        (tester) async {
      final c = KycFlowController(
        service: _FailingSubmitService(),
        documentValidator: (image) async =>
            const KycDocumentValidation(isValid: true),
      );
      addTearDown(c.dispose);
      await _pumpStart(tester, c);

      // F-T44+F-T45: se alcanza el submit por el flujo documentado
      // (lookup válido -> captura -> validación -> liveness -> submit).
      await _fillValidateAndContinue(tester);
      await tester.tap(find.byKey(const Key('captureDocumentButton')));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.byKey(const Key('kycDocumentContinue')));
      await tester.tap(find.byKey(const Key('kycDocumentContinue')));
      await tester.pumpAndSettle();
      await _tapTaskButton(tester, 'Capturar');
      await _tapTaskButton(tester, 'Capturar');
      await _tapTaskButton(tester, 'Enviar verificación');
      await tester.pumpAndSettle();

      // F-T47 (decisión del dueño): el `overall_result=false` ya no muestra
      // el flujo de reintento con el estado anterior; reinicia TODO en
      // silencio y vuelve a `/kyc` para empezar de nuevo.
      expect(find.byKey(const Key('kycBackConfirmDialog')), findsNothing);
      expect(find.text('Empecemos por ti'), findsOneWidget);
      expect(c.challenge, isNull);
      expect(c.result, isNull);
      expect(c.documentType, 'DNI');
      expect(c.documentNumber, isEmpty);
      expect(c.applicant, isNull);
      expect(c.manualReviewFolio, isNull);
    });
  });
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
      const KycSubmitResult(
        overallResult: false,
        detailCode: 'LIVENESS_FAILED',
        failedStep: 'blink',
        overallReason: 'INSUFFICIENT_MOVEMENT',
      );
}
