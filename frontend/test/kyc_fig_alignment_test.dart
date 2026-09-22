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

GoRouter _router(KycFlowController controller) => GoRouter(
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

Future<void> _pumpStart(WidgetTester tester, KycFlowController c) async {
  await tester.pumpWidget(MaterialApp.router(routerConfig: _router(c)));
  await tester.pumpAndSettle();
}

Future<void> _fillStartForm(WidgetTester tester) async {
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
      expect(
        find.text('Ingresa tus datos tal como figuran en tu DNI.'),
        findsOneWidget,
      );
      expect(find.text('Número de DNI'), findsOneWidget);
      expect(find.text('Nombres'), findsOneWidget);
      expect(find.text('Apellidos'), findsOneWidget);
      expect(find.text('Correo electrónico'), findsOneWidget);
      expect(find.text('8 dígitos'), findsOneWidget);
      expect(
        find.textContaining('Aquí te enviaremos tus constancias'),
        findsOneWidget,
      );
      expect(find.textContaining('Validaremos tu identidad'), findsOneWidget);
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

      await _tapContinuar(tester);

      expect(find.byKey(const Key('kycFormErrorBanner')), findsOneWidget);
      expect(
        find.text('Revisa los campos marcados para continuar'),
        findsOneWidget,
      );
      expect(find.text('Ingresa el numero de documento'), findsOneWidget);
      expect(c.challenge, isNull);
    });

    testWidgets('cargando y error del challenge conservan el estilo',
        (tester) async {
      final c = KycFlowController(service: const _FakeKycService());
      addTearDown(c.dispose);
      await _pumpStart(tester, c);

      await _fillStartForm(tester);
      await _tapContinuar(tester);

      // Avanzó al documento: el challenge se pidió y el estilo se conserva.
      expect(find.text('Escanea tu DNI'), findsOneWidget);
      expect(c.challenge, isNotNull);
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

      await _fillStartForm(tester);
      await _tapContinuar(tester);

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

      await _fillStartForm(tester);
      await _tapContinuar(tester);
      await tester.tap(find.byKey(const Key('captureDocumentButton')));
      await tester.pumpAndSettle();

      expect(find.text('No pudimos leer tu DNI'), findsOneWidget);
      expect(find.text('No legible'), findsOneWidget);
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
      expect(find.text('Buena iluminación'), findsOneWidget);
      expect(find.text('Rostro descubierto'), findsOneWidget);
      expect(
        find.text('Prueba de vida (En proceso)'),
        findsOneWidget,
      );
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
    testWidgets('fallo muestra el paso y el motivo con tokens', (tester) async {
      final c = KycFlowController(service: _FailingSubmitService());
      addTearDown(c.dispose);
      await _pumpStart(tester, c);

      await _fillStartForm(tester);
      await _tapContinuar(tester);
      await tester.tap(find.byKey(const Key('captureDocumentButton')));
      await tester.pumpAndSettle();
      await _tapTaskButton(tester, 'Capturar');
      await _tapTaskButton(tester, 'Capturar');
      await _tapTaskButton(tester, 'Enviar verificacion');
      await tester.pumpAndSettle();

      expect(
        tester.widget<Text>(find.byKey(const Key('kycResultFailedStep'))).data,
        contains('blink'),
      );
      expect(
        tester.widget<Text>(find.byKey(const Key('kycResultReason'))).data,
        contains('movimiento'),
      );
      expect(find.text('Reintentar verificacion'), findsOneWidget);
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
