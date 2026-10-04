import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/accounts/models/account.dart';
import 'package:banca_online/features/beneficiaries/data/beneficiaries_service.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/beneficiaries/presentation/beneficiaries_page.dart';
import 'package:banca_online/features/beneficiaries/presentation/beneficiary_form_page.dart';
import 'package:banca_online/features/beneficiaries/utils/account_number.dart';
import 'package:banca_online/features/transfers/data/transfers_service.dart';
import 'package:banca_online/features/transfers/models/resolve_target.dart';
import 'package:banca_online/features/transfers/models/transfer.dart';
import 'package:banca_online/features/transfers/presentation/amount_page.dart';
import 'package:banca_online/features/transfers/presentation/destination_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

// Numeros validos (DV modulo 11 + prefijo 001, E2-T28/E2-T29):
// base `0019482710401` -> DV 8; base `0011234567890` -> DV 8.
// `1919482710401` -> DV 9 solo como vector del algoritmo (prefijo
// invalido: no aceptada como cuenta).
const _account14a = '00194827104018';
const _account14b = '00112345678908';
const _account14BadDv = '00194827104010';
const _cci20 = '00219100284729104458';

const _a1 = Account(
  id: 'a1',
  accountNumberMasked: '•••• 8921',
  type: 'SAVINGS',
  currency: 'PEN',
  availableMinor: 485020,
  heldMinor: 0,
  balanceMinor: 485020,
);

const _resolvedDni = ResolvedTarget(
  targetAccountId: 'acc-dni-1',
  holderNameMasked: 'J*** P***',
  accountNumberMasked: '•••• 3318',
  currency: 'PEN',
);

const _resolvedPhone = ResolvedTarget(
  targetAccountId: 'acc-phone-1',
  holderNameMasked: 'M*** R***',
  accountNumberMasked: '•••• 7744',
  currency: 'PEN',
);

/// Fake minimo de beneficiarios (solo alta).
class _FakeBeneficiaries implements BeneficiariesServiceBase {
  BeneficiaryInput? lastCreated;

  @override
  Future<Beneficiary> create(
    BeneficiaryInput input, {
    String? idempotencyKey,
  }) async {
    lastCreated = input;
    return const Beneficiary(
      id: 'b-new',
      alias: 'X',
      accountMasked: '•••• 0018',
      bankCode: 'BCP',
      holderName: '••••',
      currency: 'PEN',
    );
  }

  @override
  Future<List<Beneficiary>> list({String? query}) async =>
      const <Beneficiary>[];

  @override
  Future<Beneficiary> update(String id, BeneficiaryInput input) =>
      throw UnimplementedError();

  @override
  Future<Beneficiary> remove(String id) => throw UnimplementedError();
}

Widget _beneficiaryHarness(_FakeBeneficiaries fake) {
  final router = GoRouter(
    initialLocation: '/beneficiaries/new',
    routes: [
      GoRoute(
        path: '/beneficiaries',
        builder: (context, state) => BeneficiariesPage(service: fake),
      ),
      GoRoute(
        path: '/beneficiaries/new',
        builder: (context, state) => BeneficiaryFormPage(service: fake),
      ),
    ],
  );
  return MaterialApp.router(routerConfig: router);
}

Future<void> _fillAliasAndDoc(WidgetTester tester) async {
  await tester.enterText(
    find.byKey(const Key('beneficiary-alias')),
    'Primo Lima',
  );
  await tester.enterText(
    find.byKey(const Key('beneficiary-doc-number')),
    '42891044',
  );
  await tester.pump();
}

/// Fake de transferencias con lookup previo configurable.
class _FakeTransfers implements TransfersServiceBase {
  ResolvedTarget? resolveResult;
  ApiException? resolveError;
  int resolveCalls = 0;
  String? lastDocType;
  String? lastDocNumber;
  String? lastPhone;

  @override
  Future<List<Account>> accounts() async => const [_a1];

  @override
  Future<List<Beneficiary>> beneficiaries({String? query}) async => const [];

  @override
  Future<ResolvedTarget> resolveTarget({
    String? documentType,
    String? documentNumber,
    String? phone,
  }) async {
    resolveCalls++;
    lastDocType = documentType;
    lastDocNumber = documentNumber;
    lastPhone = phone;
    if (resolveError != null) throw resolveError!;
    return resolveResult ?? _resolvedDni;
  }

  @override
  Future<TransferResult> createOwnTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) => throw UnimplementedError();

  @override
  Future<TransferResult> createThirdPartyTransfer({
    required String sourceAccountId,
    required String targetAccountId,
    required int amountMinor,
    String currency = 'PEN',
    int? feeMinor,
    String? beneficiaryId,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) => throw UnimplementedError();

  @override
  Future<TransferResult> createInterbankTransfer({
    required String sourceAccountId,
    required int amountMinor,
    required String cci,
    String currency = 'PEN',
    String? holderName,
    String? bankCode,
    int? feeMinor,
    Map<String, dynamic>? metadata,
    String? idempotencyKey,
  }) => throw UnimplementedError();

  @override
  Future<TransactionDetail> getTransaction(String transactionId) =>
      throw UnimplementedError();

  @override
  Future<ReceiptResult> getReceipt(String transactionId) =>
      throw UnimplementedError();
}

Widget _destinationHarness(
  _FakeTransfers fake, {
  void Function(TransferFlowArgs)? onAmount,
}) {
  final router = GoRouter(
    initialLocation: '/transfers/destination?kind=third',
    routes: [
      GoRoute(
        path: '/transfers/destination',
        builder: (context, state) =>
            DestinationPage(kind: TransferKind.thirdParty, service: fake),
      ),
      GoRoute(
        path: '/transfers/amount',
        builder: (context, state) {
          final args = state.extra as TransferFlowArgs;
          onAmount?.call(args);
          return AmountPage(args: args);
        },
      ),
    ],
  );
  return MaterialApp.router(routerConfig: router);
}

Future<void> _pumpDestination(
  WidgetTester tester,
  _FakeTransfers fake, {
  void Function(TransferFlowArgs)? onAmount,
}) async {
  await tester.pumpWidget(_destinationHarness(fake, onAmount: onAmount));
  await tester.pumpAndSettle();
}

void main() {
  group('E2-T29 A: formato de cuenta 14+DV / 20 CCI', () {
    test('DV modulo 11: bases conocidas dan 8 y 9', () {
      expect(accountCheckDigit('0019482710401'), 8);
      expect(accountCheckDigit('1919482710401'), 9);
      expect(accountCheckDigit('0011234567890'), 8);
      expect(accountCheckDigit('corta'), -1);
    });

    test('cuenta 14 con DV valido aceptada; DV invalido rechazado', () {
      expect(isValidAccountOrCciFormat(_account14a), isTrue);
      expect(isValidAccountOrCciFormat(_account14b), isTrue);
      expect(isValidAccountOrCciFormat(_account14BadDv), isFalse);
      expect(accountFormatError(_account14a), isNull);
      expect(
        accountFormatError(_account14BadDv),
        contains('Dígito verificador'),
      );
    });

    test('prefijo distinto de 001 se rechaza aunque el DV sea correcto', () {
      // `19194827104019` trae DV 9 correcto para su base, pero prefijo
      // `191`: el backend exige `^001[0-9]{11}$` (E2-T28) y el cliente
      // debe alinearse (regresion E2-T29).
      expect(accountCheckDigit('1919482710401'), 9);
      expect(isValidAccountCheckDigit('19194827104019'), isFalse);
      expect(isValidAccountOrCciFormat('19194827104019'), isFalse);
      expect(accountFormatError('19194827104019'), contains('empieza en 001'));
      // Prefijo ok + DV malo sigue reportando DV (no prefijo).
      expect(
        accountFormatError(_account14BadDv),
        contains('Dígito verificador'),
      );
    });

    test('CCI 20 aceptado, con guiones y espacios normalizados', () {
      expect(isValidAccountOrCciFormat(_cci20), isTrue);
      expect(isValidAccountOrCciFormat('002-191-002847291044-58'), isTrue);
      expect(isValidAccountOrCciFormat('0021 9100 2847 2910 4458'), isTrue);
      expect(accountFormatError(_cci20), isNull);
      expect(normalizeAccountOrCci('0019-482710401-8'), _account14a);
    });

    test('formatos invalidos rechazados: corto, largo intermedio, letras', () {
      expect(isValidAccountOrCciFormat('12345678'), isFalse);
      expect(isValidAccountOrCciFormat('1234567890123'), isFalse);
      expect(isValidAccountOrCciFormat('123456789012345'), isFalse);
      expect(isValidAccountOrCciFormat('0019482710401A'), isFalse);
      expect(isValidAccountOrCciFormat(''), isFalse);
      expect(
        accountFormatError('123'),
        'Ingresa 14 dígitos (cuenta) o 20 (CCI).',
      );
      expect(
        accountFormatError('0019482710401A'),
        'Ingresa 14 dígitos (cuenta) o 20 (CCI).',
      );
    });

    test('validadores DNI/celular: longitudes y prefijo', () {
      expect(isValidDniFormat('42891044'), isTrue);
      expect(isValidDniFormat('104289104'), isTrue);
      expect(isValidDniFormat('123'), isFalse);
      expect(isValidDniFormat('1234567890'), isFalse);
      expect(isValidPhoneFormat('987654321'), isTrue);
      expect(isValidPhoneFormat('887654321'), isFalse);
      expect(isValidPhoneFormat('98765432'), isFalse);
      expect(dniFormatError('123'), contains('8 o 9'));
      expect(phoneFormatError('887654321'), contains('empieza con 9'));
    });
  });

  group('E2-T29 A: formulario de beneficiario', () {
    testWidgets('ayuda indica 14 (cuenta) o 20 (CCI)', (tester) async {
      await tester.pumpWidget(_beneficiaryHarness(_FakeBeneficiaries()));
      await tester.pumpAndSettle();

      expect(find.text('14 dígitos (cuenta) o 20 (CCI)'), findsOneWidget);
      expect(find.textContaining('entre 8 y 20'), findsNothing);
    });

    testWidgets('alta con cuenta 14 valida envia el numero normalizado', (
      tester,
    ) async {
      final fake = _FakeBeneficiaries();
      await tester.pumpWidget(_beneficiaryHarness(fake));
      await tester.pumpAndSettle();
      await _fillAliasAndDoc(tester);
      await tester.enterText(
        find.byKey(const Key('beneficiary-account')),
        '0019-482710401-8',
      );
      await tester.pump();

      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(fake.lastCreated, isNotNull);
      expect(fake.lastCreated!.accountOrCci, _account14a);
    });

    testWidgets('alta con CCI 20 aceptada', (tester) async {
      final fake = _FakeBeneficiaries();
      await tester.pumpWidget(_beneficiaryHarness(fake));
      await tester.pumpAndSettle();
      await _fillAliasAndDoc(tester);
      await tester.enterText(
        find.byKey(const Key('beneficiary-account')),
        _cci20,
      );
      await tester.pump();

      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(fake.lastCreated, isNotNull);
      expect(fake.lastCreated!.accountOrCci, _cci20);
    });

    testWidgets('DV invalido bloquea el guardado con error bajo el campo', (
      tester,
    ) async {
      final fake = _FakeBeneficiaries();
      await tester.pumpWidget(_beneficiaryHarness(fake));
      await tester.pumpAndSettle();
      await _fillAliasAndDoc(tester);
      await tester.enterText(
        find.byKey(const Key('beneficiary-account')),
        _account14BadDv,
      );
      await tester.pump();

      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pump();

      expect(
        find.textContaining('Dígito verificador de cuenta incorrecto'),
        findsOneWidget,
      );
      expect(fake.lastCreated, isNull);
    });
  });

  group('E2-T29 B: resolve-target (servicio HTTP)', () {
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

    Response<dynamic> okResolve(RequestOptions o) => Response(
      requestOptions: o,
      statusCode: 200,
      data: {
        'data': {
          'target_account_id': 'acc-1',
          'holder_name_masked': 'J*** P***',
          'account_number_masked': '•••• 3318',
          'currency': 'PEN',
        },
        'meta': {'request_id': 'r1'},
      },
    );

    test(
      'DNI envia target_document sin Idempotency-Key y parsea mascaras',
      () async {
        Map<String, dynamic>? body;
        Map<String, dynamic>? headers;
        String? path;
        final api = stubClient(
          okResolve,
          onRequest: (o) {
            path = o.path;
            headers = Map<String, dynamic>.from(o.headers);
            if (o.data is Map) {
              body = Map<String, dynamic>.from(o.data as Map);
            }
            return null;
          },
        );

        final resolved = await TransfersService(api: api)
            .resolveTarget(documentType: 'DNI', documentNumber: '42891044');

        expect(path, contains('resolve-target'));
        expect((body!['target_document'] as Map)['number'], '42891044');
        expect((body!['target_document'] as Map)['type'], 'DNI');
        expect(body, isNot(contains('target_phone')));
        expect(headers?['Idempotency-Key'], isNull);
        expect(resolved.targetAccountId, 'acc-1');
        expect(resolved.holderNameMasked, 'J*** P***');
        expect(resolved.accountNumberMasked, '•••• 3318');
      },
    );

    test('celular envia target_phone sin Idempotency-Key', () async {
      Map<String, dynamic>? body;
      final api = stubClient(
        okResolve,
        onRequest: (o) {
          if (o.data is Map) {
            body = Map<String, dynamic>.from(o.data as Map);
          }
          return null;
        },
      );

      final resolved = await TransfersService(api: api)
          .resolveTarget(phone: '987654321');

      expect(body!['target_phone'], '987654321');
      expect(body, isNot(contains('target_document')));
      expect(resolved.targetAccountId, 'acc-1');
    });

    test('ambos o ninguno fallan localmente sin salir a red', () async {
      var calls = 0;
      final api = stubClient((o) {
        calls++;
        return okResolve(o);
      });

      await expectLater(
        TransfersService(api: api)
            .resolveTarget(documentNumber: '42891044', phone: '987654321'),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'VALIDATION_ERROR'),
        ),
      );
      await expectLater(
        TransfersService(api: api).resolveTarget(),
        throwsA(
          isA<ApiException>().having((e) => e.code, 'code', 'VALIDATION_ERROR'),
        ),
      );
      expect(calls, 0);
    });

    test(
      'TARGET_NOT_FOUND neutro y TARGET_AMBIGUOUS accionable, sin PII',
      () async {
        String msg(String code) =>
            transferUserMessage(ApiException(code: code, message: 'srv'));

        expect(msg('TARGET_NOT_FOUND'), kTargetNotFoundMessage);
        expect(msg('TARGET_NOT_FOUND'), contains('Verifica los datos'));
        expect(msg('TARGET_AMBIGUOUS'), kTargetAmbiguousMessage);
        expect(msg('TARGET_AMBIGUOUS'), contains('número de cuenta'));
        // Sin eco de datos sensibles en los mensajes fijos.
        expect(msg('TARGET_NOT_FOUND'), isNot(contains('42891044')));
        expect(msg('TARGET_AMBIGUOUS'), isNot(contains('987654321')));
      },
    );
  });

  group('E2-T29 B: destino por DNI/celular en el flujo', () {
    testWidgets('modo DNI: lookup muestra titular y continua con el id', (
      tester,
    ) async {
      final fake = _FakeTransfers()..resolveResult = _resolvedDni;
      TransferFlowArgs? captured;
      await _pumpDestination(tester, fake, onAmount: (a) => captured = a);

      await tester.tap(find.byKey(const Key('dest-mode-dni')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('dest-dni-input')),
        '42891044',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('dest-lookup-button')));
      await tester.pumpAndSettle();

      expect(fake.resolveCalls, 1);
      expect(fake.lastDocNumber, '42891044');
      expect(fake.lastDocType, 'DNI');
      expect(find.byKey(const Key('dest-resolved-card')), findsOneWidget);
      expect(find.text('J*** P***'), findsOneWidget);
      expect(find.textContaining('•••• 3318'), findsOneWidget);

      await tester.tap(find.byKey(const Key('destination-continue')));
      await tester.pumpAndSettle();

      expect(captured, isNotNull);
      expect(captured!.draft.targetAccountId, 'acc-dni-1');
      expect(captured!.draft.beneficiaryAlias, 'J*** P***');
      expect(find.text('J*** P***'), findsWidgets);
    });

    testWidgets('modo celular: lookup con target_phone y continua', (
      tester,
    ) async {
      final fake = _FakeTransfers()..resolveResult = _resolvedPhone;
      TransferFlowArgs? captured;
      await _pumpDestination(tester, fake, onAmount: (a) => captured = a);

      await tester.tap(find.byKey(const Key('dest-mode-phone')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('dest-phone-input')),
        '987654321',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('dest-lookup-button')));
      await tester.pumpAndSettle();

      expect(fake.lastPhone, '987654321');
      expect(find.text('M*** R***'), findsOneWidget);
      await tester.tap(find.byKey(const Key('destination-continue')));
      await tester.pumpAndSettle();

      expect(captured!.draft.targetAccountId, 'acc-phone-1');
    });

    testWidgets('TARGET_NOT_FOUND neutro sin pistas ni PII', (tester) async {
      final fake = _FakeTransfers()
        ..resolveError = ApiException(
          code: 'TARGET_NOT_FOUND',
          message: 'srv con 42891044',
        );
      await _pumpDestination(tester, fake);

      await tester.tap(find.byKey(const Key('dest-mode-dni')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('dest-dni-input')),
        '42891044',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('dest-lookup-button')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('dest-lookup-error')), findsOneWidget);
      expect(find.text(kTargetNotFoundMessage), findsOneWidget);
      // El mensaje neutro no porta el documento (sin PII en UI/errores).
      final errText = tester.widget<Text>(
        find.byKey(const Key('dest-lookup-error')),
      );
      expect(errText.data ?? '', isNot(contains('42891044')));
      // Sin titular resuelto no se puede continuar.
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('destination-continue')))
            .enabled,
        isFalse,
      );
    });

    testWidgets('TARGET_AMBIGUOUS pide usar el numero de cuenta', (
      tester,
    ) async {
      final fake = _FakeTransfers()
        ..resolveError = ApiException(code: 'TARGET_AMBIGUOUS', message: 'srv');
      await _pumpDestination(tester, fake);

      await tester.tap(find.byKey(const Key('dest-mode-phone')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('dest-phone-input')),
        '987654321',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('dest-lookup-button')));
      await tester.pumpAndSettle();

      expect(find.text(kTargetAmbiguousMessage), findsOneWidget);
      final ambText = tester.widget<Text>(
        find.byKey(const Key('dest-lookup-error')),
      );
      expect(ambText.data ?? '', isNot(contains('987654321')));
    });

    testWidgets('formato invalido bloquea el lookup y cambiar limpia', (
      tester,
    ) async {
      final fake = _FakeTransfers()..resolveResult = _resolvedDni;
      await _pumpDestination(tester, fake);

      await tester.tap(find.byKey(const Key('dest-mode-dni')));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const Key('dest-dni-input')), '123');
      await tester.pump();
      await tester.tap(find.byKey(const Key('dest-lookup-button')));
      await tester.pump();

      expect(find.text('El DNI tiene 8 o 9 dígitos.'), findsOneWidget);
      expect(fake.resolveCalls, 0);

      // Lookup valido y luego cambio de modo: se limpia todo.
      await tester.enterText(
        find.byKey(const Key('dest-dni-input')),
        '42891044',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('dest-lookup-button')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('dest-resolved-card')), findsOneWidget);

      await tester.tap(find.byKey(const Key('dest-mode-phone')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('dest-resolved-card')), findsNothing);
      expect(find.byKey(const Key('dest-phone-input')), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('destination-continue')))
            .enabled,
        isFalse,
      );
    });

    testWidgets('editar DNI tras lookup limpia el resuelto (stale)', (
      tester,
    ) async {
      final fake = _FakeTransfers()..resolveResult = _resolvedDni;
      await _pumpDestination(tester, fake);

      await tester.tap(find.byKey(const Key('dest-mode-dni')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('dest-dni-input')),
        '42891044',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('dest-lookup-button')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('dest-resolved-card')), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('destination-continue')))
            .enabled,
        isTrue,
      );

      // Editar el campo invalida el resuelto hasta volver a buscar.
      await tester.enterText(
        find.byKey(const Key('dest-dni-input')),
        '42891045',
      );
      await tester.pump();
      expect(find.byKey(const Key('dest-resolved-card')), findsNothing);
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('destination-continue')))
            .enabled,
        isFalse,
      );
      expect(fake.resolveCalls, 1);
    });

    testWidgets('editar celular tras lookup limpia el resuelto (stale)', (
      tester,
    ) async {
      final fake = _FakeTransfers()..resolveResult = _resolvedPhone;
      await _pumpDestination(tester, fake);

      await tester.tap(find.byKey(const Key('dest-mode-phone')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('dest-phone-input')),
        '987654321',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('dest-lookup-button')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('dest-resolved-card')), findsOneWidget);

      await tester.enterText(
        find.byKey(const Key('dest-phone-input')),
        '987654322',
      );
      await tester.pump();
      expect(find.byKey(const Key('dest-resolved-card')), findsNothing);
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('destination-continue')))
            .enabled,
        isFalse,
      );
      expect(fake.resolveCalls, 1);
    });
  });
}
