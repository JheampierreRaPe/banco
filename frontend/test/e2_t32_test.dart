// Pruebas E2-T32 (HU07/HU08): tope pre-aprobado como techo absoluto de
// transferencia rapida.
//
// - El formulario admite topes por encima del umbral (presets S/1,000 y
//   S/2,000 + entrada libre).
// - El copy refleja la semantica real (techo rapido sin biometria, incluso
//   por encima del control general; el limite diario siempre aplica).
// - Cliente delgado: la app solo fija el tope y lo envia; no decide la
//   exencion.
import 'package:banca_online/features/beneficiaries/data/beneficiaries_service.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/beneficiaries/presentation/beneficiary_form_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

class FakeBeneficiariesService implements BeneficiariesServiceBase {
  BeneficiaryInput? lastCreated;
  String? lastUpdatedId;
  BeneficiaryInput? lastUpdated;

  @override
  Future<List<Beneficiary>> list({String? query}) async => const [];

  @override
  Future<Beneficiary> create(
    BeneficiaryInput input, {
    String? idempotencyKey,
  }) async {
    lastCreated = input;
    return Beneficiary(
      id: 'b-new',
      alias: input.alias,
      accountMasked: '•••• 0012',
      bankCode: input.bankCode ?? '',
      holderName: '••••',
      preapprovedLimitMinor: input.preapprovedLimitMinor,
      currency: input.currency,
    );
  }

  @override
  Future<Beneficiary> update(String id, BeneficiaryInput input) async {
    lastUpdatedId = id;
    lastUpdated = input;
    return Beneficiary(
      id: id,
      alias: input.alias,
      accountMasked: '•••• 0012',
      bankCode: input.bankCode ?? '',
      holderName: '••••',
      preapprovedLimitMinor: input.preapprovedLimitMinor,
      currency: input.currency,
    );
  }

  @override
  Future<Beneficiary> remove(String id) async {
    throw UnimplementedError();
  }
}

GoRouter _router(FakeBeneficiariesService fake) {
  return GoRouter(
    initialLocation: '/beneficiaries/new',
    routes: [
      GoRoute(
        path: '/beneficiaries',
        builder: (context, state) =>
            const Scaffold(body: Text('Agenda de Beneficiarios')),
      ),
      GoRoute(
        path: '/beneficiaries/new',
        builder: (context, state) => BeneficiaryFormPage(service: fake),
      ),
      GoRoute(
        path: '/beneficiaries/:id',
        builder: (context, state) {
          final id = state.pathParameters['id'] ?? '';
          final extra = state.extra;
          return BeneficiaryFormPage(
            service: fake,
            beneficiaryId: id,
            initial: extra is Beneficiary ? extra : null,
          );
        },
      ),
    ],
  );
}

Future<void> _fillValidForm(WidgetTester tester) async {
  await tester.enterText(
    find.byKey(const Key('beneficiary-alias')),
    'Mamá Lima',
  );
  await tester.enterText(
    find.byKey(const Key('beneficiary-doc-number')),
    '42891044',
  );
  await tester.enterText(
    find.byKey(const Key('beneficiary-account')),
    '00112345678908',
  );
  await tester.pump();
}

void main() {
  group('E2-T32 techo pre-aprobado (CA-05)', () {
    testWidgets('presets por encima del umbral guardan el tope alto', (
      tester,
    ) async {
      final fake = FakeBeneficiariesService();
      await tester.pumpWidget(
        MaterialApp.router(routerConfig: _router(fake)),
      );
      await tester.pumpAndSettle();
      await _fillValidForm(tester);

      // Presets >= S/1,000 disponibles (por encima del umbral S/1,000).
      expect(find.byKey(const Key('tope-1000')), findsOneWidget);
      expect(find.byKey(const Key('tope-2000')), findsOneWidget);
      await tester.ensureVisible(find.byKey(const Key('tope-2000')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('tope-2000')));
      await tester.pump();

      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(fake.lastCreated, isNotNull);
      // S/2,000 en centimos: techo por encima del umbral.
      expect(fake.lastCreated!.preapprovedLimitMinor, 200000);
    });

    testWidgets('copy describe el techo rapido y el limite diario', (
      tester,
    ) async {
      final fake = FakeBeneficiariesService();
      await tester.pumpWidget(
        MaterialApp.router(routerConfig: _router(fake)),
      );
      await tester.pumpAndSettle();

      expect(
        find.text('Transferencia rápida sin biometría'),
        findsOneWidget,
      );
      expect(
        find.textContaining('transferencia rápida sin biometría'),
        findsWidgets,
      );
      expect(
        find.textContaining('por encima del control general'),
        findsOneWidget,
      );
      expect(
        find.textContaining('límite diario siempre aplica'),
        findsOneWidget,
      );
      // El copy viejo ("1 toque") ya no se muestra.
      expect(find.textContaining('1 toque'), findsNothing);
    });

    testWidgets('entrada libre admite tope por encima del umbral', (
      tester,
    ) async {
      final fake = FakeBeneficiariesService();
      await tester.pumpWidget(
        MaterialApp.router(routerConfig: _router(fake)),
      );
      await tester.pumpAndSettle();
      await _fillValidForm(tester);

      await tester.ensureVisible(find.byKey(const Key('tope-custom')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('tope-custom')));
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('tope-custom-value')),
        '1500',
      );
      await tester.pump();

      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(fake.lastCreated, isNotNull);
      expect(fake.lastCreated!.preapprovedLimitMinor, 150000);
    });

    testWidgets('edicion reconoce un tope alto como preset (no libre)', (
      tester,
    ) async {
      final fake = FakeBeneficiariesService();
      final router = _router(fake);
      await tester.pumpWidget(MaterialApp.router(routerConfig: router));
      await tester.pumpAndSettle();

      router.push(
        '/beneficiaries/b9',
        extra: const Beneficiary(
          id: 'b9',
          alias: 'Mamá Lima',
          accountMasked: '•••• 0012',
          bankCode: 'BCP',
          holderName: '••••',
          preapprovedLimitMinor: 200000,
          currency: 'PEN',
        ),
      );
      await tester.pumpAndSettle();

      // Preset detectado: no pide monto libre y muestra el tope actual.
      expect(find.byKey(const Key('tope-custom-value')), findsNothing);
      expect(find.textContaining('2000 por operación'), findsOneWidget);
    });
  });
}
