import 'dart:typed_data';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/theme/app_colors.dart';
import 'package:banca_online/core/widgets/app_button.dart';
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_document_page.dart';
import 'package:banca_online/features/kyc/presentation/kyc_start_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Doble del lookup de titular (E1-T35): sin red, sin PII real.
class FakeDocumentLookup implements KycDocumentLookupService {
  FakeDocumentLookup({this.owner, this.error});

  KycDocumentOwner? owner;
  Object? error;
  String? lastType;
  String? lastNumber;
  int calls = 0;

  @override
  Future<KycDocumentOwner> lookupDocument({
    required String type,
    required String number,
  }) async {
    calls++;
    lastType = type;
    lastNumber = number;
    final failure = error;
    if (failure != null) throw failure;
    return owner ??
        const KycDocumentOwner(
          documentType: 'DNI',
          firstName: 'Juan Carlos',
          lastName: 'Perez Garcia',
        );
  }
}

/// Solo challenge (el submit no se usa en esta pantalla).
class _ChallengeOnlyService implements KycService {
  const _ChallengeOnlyService();

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
      throw UnimplementedError('fuera de la frontera F-T44');
}

GoRouter _testRouter(
  KycFlowController controller,
  KycDocumentLookupService lookup,
) =>
    GoRouter(
      initialLocation: '/kyc',
      routes: [
        GoRoute(
          path: '/kyc',
          builder: (context, state) => KycStartPage(
            controller: controller,
            lookupService: lookup,
          ),
        ),
        GoRoute(
          path: '/kyc/document',
          builder: (context, state) => KycDocumentPage(controller: controller),
        ),
      ],
    );

Future<void> _pumpStart(
  WidgetTester tester,
  KycFlowController controller,
  KycDocumentLookupService lookup,
) async {
  await tester.pumpWidget(
    MaterialApp.router(
      routerConfig: _testRouter(controller, lookup),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _tapValidar(WidgetTester tester) async {
  await tester.ensureVisible(find.byKey(const Key('validateDocumentButton')));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const Key('validateDocumentButton')));
  await tester.pumpAndSettle();
}

bool _continuarEnabled(WidgetTester tester) {
  final button = tester.widget<AppPrimaryButton>(
    find.widgetWithText(AppPrimaryButton, 'Continuar'),
  );
  return button.onPressed != null;
}

Future<void> _selectRuc(WidgetTester tester) async {
  await tester.ensureVisible(find.byKey(const Key('docTypeField')));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const Key('docTypeField')));
  await tester.pumpAndSettle();
  await tester.tap(find.text('RUC'));
  await tester.pumpAndSettle();
}

/// `true` si el campo es no editable (el `readOnly` vive en el `TextField`
/// interno que construye el `TextFormField`).
bool _isReadOnly(WidgetTester tester, String key) {
  final inner = find.descendant(
    of: find.byKey(Key(key)),
    matching: find.byType(TextField),
  );
  expect(inner, findsOneWidget, reason: 'TextField interno de $key');
  return tester.widget<TextField>(inner).readOnly;
}

void main() {
  testWidgets('F-T44: orden y textos crearCuenta-1Datos (CA-01/CA-02/CA-05)',
      (tester) async {
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, FakeDocumentLookup());

    // Textos canonicos con "documento" (no "DNI" generico).
    expect(find.text('Tipo de documento'), findsOneWidget);
    expect(find.text('Opciones: DNI · RUC'), findsOneWidget);
    expect(find.text('Número de documento'), findsOneWidget);
    expect(find.text('Solo números · 8 dígitos'), findsOneWidget);
    expect(find.text('Validar documento'), findsOneWidget);
    expect(find.text('Nombres'), findsOneWidget);
    expect(find.text('Apellidos'), findsOneWidget);
    expect(find.text('Correo electrónico'), findsOneWidget);
    expect(find.text('Teléfono (opcional)'), findsOneWidget);
    expect(
      find.text('Ingresa tus datos tal como figuran en tu documento.'),
      findsOneWidget,
    );
    expect(
      find.text(
        'Validaremos tu identidad con una foto de tu documento y '
        'reconocimiento facial.',
      ),
      findsOneWidget,
    );
    // Default DNI.
    expect(find.text('DNI'), findsOneWidget);
    // Nombres/Apellidos no editables (solo la API los llena).
    expect(_isReadOnly(tester, 'firstNameField'), isTrue);
    expect(_isReadOnly(tester, 'lastNameField'), isTrue);

    // Orden vertical del fig: tipo -> numero -> validar -> nombres ->
    // apellidos -> correo -> telefono.
    final offsets = <String, double>{
      for (final key in [
        'docTypeField',
        'docNumberField',
        'validateDocumentButton',
        'firstNameField',
        'lastNameField',
        'emailField',
        'phoneField',
      ])
        key: tester.getTopLeft(find.byKey(Key(key))).dy,
    };
    final ordered = offsets.values.toList();
    expect(ordered, orderedEquals(ordered..sort()));
    expect(offsets['docTypeField']! < offsets['docNumberField']!, isTrue);
    expect(
      offsets['docNumberField']! < offsets['validateDocumentButton']!,
      isTrue,
    );
    expect(
      offsets['validateDocumentButton']! < offsets['firstNameField']!,
      isTrue,
    );
    expect(offsets['firstNameField']! < offsets['lastNameField']!, isTrue);
    expect(offsets['lastNameField']! < offsets['emailField']!, isTrue);
    expect(offsets['emailField']! < offsets['phoneField']!, isTrue);
  });

  testWidgets('F-T44: Continuar y Validar bloqueados antes de validar',
      (tester) async {
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, FakeDocumentLookup());

    expect(_continuarEnabled(tester), isFalse);
    final validar = tester.widget<AppPrimaryButton>(
      find.widgetWithText(AppPrimaryButton, 'Validar documento'),
    );
    expect(validar.onPressed, isNull);
    expect(controller.challenge, isNull);
  });

  testWidgets(
      'F-T44: Validar rellena Nombres/Apellidos (readOnly) y habilita '
      'Continuar (CA-03/CA-04)', (tester) async {
    final lookup = FakeDocumentLookup();
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, lookup);

    await tester.enterText(
      find.byKey(const Key('emailField')),
      'juan@example.com',
    );
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '12345678',
    );
    await tester.pump();
    await _tapValidar(tester);

    expect(lookup.calls, 1);
    expect(lookup.lastType, 'DNI');
    expect(lookup.lastNumber, '12345678');
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('firstNameField')))
          .controller!
          .text,
      'Juan Carlos',
    );
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('lastNameField')))
          .controller!
          .text,
      'Perez Garcia',
    );
    expect(_continuarEnabled(tester), isTrue);

    // Continuar conserva setApplicant/setDocument/loadChallenge y navega.
    await tester.ensureVisible(find.text('Continuar'));
    await tester.tap(find.text('Continuar'));
    await tester.pumpAndSettle();
    expect(controller.applicant?.firstName, 'Juan Carlos');
    expect(controller.documentNumber, '12345678');
    expect(controller.challenge?.token, 'tok-abc');
    expect(find.text('Escanea tu DNI'), findsOneWidget);
  });

  testWidgets('F-T44: RUC juridica muestra business_name en Nombres (CA-03)',
      (tester) async {
    final lookup = FakeDocumentLookup(
      owner: const KycDocumentOwner(
        documentType: 'RUC',
        businessName: 'ACME SAC',
      ),
    );
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, lookup);

    await _selectRuc(tester);
    expect(find.text('Solo números · 11 dígitos'), findsOneWidget);

    await tester.enterText(
      find.byKey(const Key('emailField')),
      'contacto@acme.pe',
    );
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '20123456789',
    );
    await tester.pump();
    await _tapValidar(tester);

    expect(lookup.lastType, 'RUC');
    expect(lookup.lastNumber, '20123456789');
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('firstNameField')))
          .controller!
          .text,
      'ACME SAC',
    );
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('lastNameField')))
          .controller!
          .text,
      isEmpty,
    );
    // Apellidos vacio no bloquea a la persona juridica.
    expect(_continuarEnabled(tester), isTrue);

    await tester.ensureVisible(find.text('Continuar'));
    await tester.tap(find.text('Continuar'));
    await tester.pumpAndSettle();
    expect(controller.documentType, 'RUC');
    expect(find.text('Escanea tu DNI'), findsOneWidget);
  });

  testWidgets('F-T44: 404 muestra mensaje neutro con reintento (CA-03)',
      (tester) async {
    final lookup = FakeDocumentLookup(
      error: ApiException(
        code: 'DOCUMENT_NOT_FOUND',
        message: messageForCode('DOCUMENT_NOT_FOUND'),
      ),
    );
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, lookup);

    await tester.enterText(
      find.byKey(const Key('emailField')),
      'juan@example.com',
    );
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '12345678',
    );
    await tester.pump();
    await _tapValidar(tester);

    expect(find.byKey(const Key('kycLookupError')), findsOneWidget);
    expect(
      find.text(messageForCode('DOCUMENT_NOT_FOUND')),
      findsOneWidget,
    );
    expect(find.text('Reintentar'), findsOneWidget);
    // Nombres vacios y Continuar bloqueado.
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('firstNameField')))
          .controller!
          .text,
      isEmpty,
    );
    expect(_continuarEnabled(tester), isFalse);
    expect(controller.challenge, isNull);
  });

  testWidgets('F-T44: error de red permite reintentar sin perder el numero',
      (tester) async {
    final lookup = FakeDocumentLookup(error: ApiException.network());
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, lookup);

    await tester.enterText(
      find.byKey(const Key('emailField')),
      'juan@example.com',
    );
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '12345678',
    );
    await tester.pump();
    await _tapValidar(tester);

    expect(find.byKey(const Key('kycLookupError')), findsOneWidget);
    expect(find.textContaining('Sin conexi'), findsOneWidget);
    // El numero sigue en el campo para reintentar.
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('docNumberField')))
          .controller!
          .text,
      '12345678',
    );

    lookup.error = null;
    await tester.ensureVisible(find.text('Reintentar'));
    await tester.tap(find.text('Reintentar'));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('kycLookupError')), findsNothing);
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('firstNameField')))
          .controller!
          .text,
      'Juan Carlos',
    );
    expect(_continuarEnabled(tester), isTrue);
  });

  testWidgets('F-T44: cambiar tipo o numero invalida lo recuperado (CA-03)',
      (tester) async {
    final lookup = FakeDocumentLookup();
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, lookup);

    await tester.enterText(
      find.byKey(const Key('emailField')),
      'juan@example.com',
    );
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '12345678',
    );
    await tester.pump();
    await _tapValidar(tester);
    expect(_continuarEnabled(tester), isTrue);

    // Cambiar el numero limpia los nombres y bloquea Continuar.
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '12345679',
    );
    await tester.pump();
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('firstNameField')))
          .controller!
          .text,
      isEmpty,
    );
    expect(_continuarEnabled(tester), isFalse);

    // Revalidar con el numero nuevo vuelve a habilitar.
    await _tapValidar(tester);
    expect(_continuarEnabled(tester), isTrue);

    // Cambiar el tipo tambien invalida.
    await _selectRuc(tester);
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('firstNameField')))
          .controller!
          .text,
      isEmpty,
    );
    expect(_continuarEnabled(tester), isFalse);
  });

  testWidgets('F-T44: el numero solo acepta digitos (CA-02)',
      (tester) async {
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, FakeDocumentLookup());

    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '12AB34CD',
    );
    await tester.pump();
    expect(
      tester
          .widget<TextFormField>(find.byKey(const Key('docNumberField')))
          .controller!
          .text,
      '1234',
    );
    // Incompleto: Validar sigue bloqueado.
    final validar = tester.widget<AppPrimaryButton>(
      find.widgetWithText(AppPrimaryButton, 'Validar documento'),
    );
    expect(validar.onPressed, isNull);
  });

  testWidgets('F-T44: Validar se habilita solo con la longitud exacta',
      (tester) async {
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, FakeDocumentLookup());

    AppPrimaryButton validar() => tester.widget<AppPrimaryButton>(
          find.widgetWithText(AppPrimaryButton, 'Validar documento'),
        );

    // DNI: 7 digitos bloqueado, 8 habilitado.
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '1234567',
    );
    await tester.pump();
    expect(validar().onPressed, isNull);
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '12345678',
    );
    await tester.pump();
    expect(validar().onPressed, isNotNull);

    // RUC: 10 digitos bloqueado, 11 habilitado.
    await _selectRuc(tester);
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '2012345678',
    );
    await tester.pump();
    expect(validar().onPressed, isNull);
    await tester.enterText(
      find.byKey(const Key('docNumberField')),
      '20123456789',
    );
    await tester.pump();
    expect(validar().onPressed, isNotNull);
  });

  testWidgets('F-T44: Validar deshabilitado aplica colores tokenizados',
      (tester) async {
    final controller =
        KycFlowController(service: const _ChallengeOnlyService());
    addTearDown(controller.dispose);
    await _pumpStart(tester, controller, FakeDocumentLookup());

    // Sin numero: Validar deshabilitado (onPressed null).
    final validar = tester.widget<AppPrimaryButton>(
      find.widgetWithText(AppPrimaryButton, 'Validar documento'),
    );
    expect(validar.onPressed, isNull);
    final inner = tester.widget<ElevatedButton>(
      find.descendant(
        of: find.widgetWithText(AppPrimaryButton, 'Validar documento'),
        matching: find.byType(ElevatedButton),
      ),
    );
    expect(
      inner.style!.backgroundColor!.resolve({WidgetState.disabled}),
      AppColors.surfaceContainerHighest,
    );
    expect(
      inner.style!.foregroundColor!.resolve({WidgetState.disabled}),
      AppColors.secondaryText,
    );
  });
}
