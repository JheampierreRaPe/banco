import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/beneficiaries/data/beneficiaries_service.dart';
import 'package:banca_online/features/beneficiaries/models/beneficiary.dart';
import 'package:banca_online/features/beneficiaries/presentation/beneficiaries_page.dart';
import 'package:banca_online/features/beneficiaries/presentation/beneficiary_form_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

/// Fake del servicio (seam `BeneficiariesServiceBase`): sin red, con datos
/// que replican los shapes E2-T13 (`GET /beneficiaries` -> `data` + `meta`).
class FakeBeneficiariesService implements BeneficiariesServiceBase {
  FakeBeneficiariesService({List<Beneficiary>? seed, this.hangList = false})
    : items = List<Beneficiary>.from(seed ?? _defaultSeed);

  static const _defaultSeed = [
    Beneficiary(
      id: 'b1',
      alias: 'Mamá Lima',
      accountMasked: '•••• 7744',
      bankCode: 'BCP',
      holderName: 'M. Rojas ••••',
      preapprovedLimitMinor: 50000,
      currency: 'PEN',
    ),
    Beneficiary(
      id: 'b2',
      alias: 'Papá Depa',
      accountMasked: '•••• 1120',
      bankCode: 'BCP',
      holderName: 'P. Alvarado ••••',
      preapprovedLimitMinor: 100000,
      currency: 'PEN',
    ),
    Beneficiary(
      id: 'b3',
      alias: 'Carlos Trabajo',
      accountMasked: '•••• 9102',
      bankCode: 'INTERBANK',
      holderName: 'C. Mendoza ••••',
      currency: 'PEN',
    ),
    Beneficiary(
      id: 'b4',
      alias: 'Lucía Gym',
      accountMasked: '•••• 4410',
      bankCode: 'BBVA',
      holderName: 'L. Fernandez ••••',
      preapprovedLimitMinor: 20000,
      currency: 'PEN',
    ),
  ];

  List<Beneficiary> items;
  final bool hangList;

  /// Ultimo `q` recibido (la busqueda la resuelve el "servidor").
  String? lastQuery;
  int listCalls = 0;

  BeneficiaryInput? lastCreated;
  String? lastUpdatedId;
  BeneficiaryInput? lastUpdated;
  String? lastRemovedId;
  int removeCalls = 0;

  /// Si no es `null`, `list` lanza este error (para probar estado error).
  ApiException? listError;

  /// Si no es `null`, `create`/`update` lanzan este error.
  ApiException? saveError;

  /// Normaliza tildes para comparar (el servidor indexa sin tildes).
  static String _plain(String value) => value
      .toLowerCase()
      .replaceAll('á', 'a')
      .replaceAll('é', 'e')
      .replaceAll('í', 'i')
      .replaceAll('ó', 'o')
      .replaceAll('ú', 'u')
      .replaceAll('ü', 'u')
      .replaceAll('ñ', 'n');

  @override
  Future<List<Beneficiary>> list({String? query}) async {
    listCalls++;
    lastQuery = query;
    if (listError != null) throw listError!;
    if (hangList) {
      await Completer<void>().future;
    }
    final q = _plain((query ?? '').trim());
    if (q.isEmpty) return List<Beneficiary>.from(items);
    // El "servidor" filtra por alias/nombre/banco (cliente delgado).
    return items
        .where(
          (b) =>
              _plain(b.alias).contains(q) ||
              _plain(b.holderName).contains(q) ||
              _plain(b.bankCode).contains(q),
        )
        .toList();
  }

  @override
  Future<Beneficiary> create(
    BeneficiaryInput input, {
    String? idempotencyKey,
  }) async {
    if (saveError != null) throw saveError!;
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
    if (saveError != null) throw saveError!;
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
    removeCalls++;
    lastRemovedId = id;
    final target = items.firstWhere((b) => b.id == id);
    items.removeWhere((b) => b.id == id);
    return target;
  }
}

/// Harness con `GoRouter` real (las paginas usan `context.push/pop`).
Widget _harness(
  FakeBeneficiariesService fake, {
  String initial = '/beneficiaries',
}) {
  final router = GoRouter(
    initialLocation: initial,
    routes: [
      GoRoute(
        path: '/beneficiaries',
        builder: (context, state) => BeneficiariesPage(service: fake),
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
  return MaterialApp.router(routerConfig: router);
}

Future<void> _pumpList(
  WidgetTester tester,
  FakeBeneficiariesService fake,
) async {
  await tester.pumpWidget(_harness(fake));
  await tester.pumpAndSettle();
}

/// Viewport alto para que el `ListView` perezoso construya todos los grupos
/// a la vez (sin scroll en los asserts).
void _tallViewport(WidgetTester tester) {
  tester.view.physicalSize = const Size(800, 1600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
}

void main() {
  group('BeneficiariesPage', () {
    testWidgets('lista agrupa por banco con topes enmascarados', (
      tester,
    ) async {
      _tallViewport(tester);
      await _pumpList(tester, FakeBeneficiariesService());

      expect(find.text('BCP · BANCO DE CRÉDITO (2)'), findsOneWidget);
      expect(find.text('INTERBANK (1)'), findsOneWidget);
      expect(find.text('BBVA PERÚ (1)'), findsOneWidget);
      expect(find.text('Mamá Lima'), findsOneWidget);
      expect(find.text('Lucía Gym'), findsOneWidget);
      expect(find.text('Tope S/ 500 (Sin biometría)'), findsOneWidget);
      expect(find.text('Tope S/ 200 (Sin biometría)'), findsOneWidget);
      // Sin tope: solo Carlos Trabajo (Lucía tiene tope S/ 200).
      expect(find.text('Sin tope (PIN/Huella)'), findsOneWidget);
      // Datos enmascarados: solo ultimos 4.
      expect(find.text('•••• 7744 · Cuenta Soles'), findsOneWidget);
    });

    testWidgets('muestra skeleton durante la carga', (tester) async {
      final fake = FakeBeneficiariesService(hangList: true);
      await tester.pumpWidget(_harness(fake));
      await tester.pump();

      expect(find.byKey(const Key('beneficiaries-skeleton')), findsOneWidget);
    });

    testWidgets('busqueda con debounce consulta al servidor (?q=)', (
      tester,
    ) async {
      final fake = FakeBeneficiariesService();
      await _pumpList(tester, fake);

      await tester.enterText(
        find.byKey(const Key('beneficiaries-search')),
        'mama',
      );
      // Antes del debounce no hay nueva consulta.
      await tester.pump(const Duration(milliseconds: 100));
      expect(fake.listCalls, 1);
      // Tras el debounce el servidor resuelve `?q=mama`.
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();

      expect(fake.lastQuery, 'mama');
      expect(find.text('Mamá Lima'), findsOneWidget);
      expect(find.text('Carlos Trabajo'), findsNothing);
    });

    testWidgets('vacio ofrece agregar beneficiario', (tester) async {
      await _pumpList(tester, FakeBeneficiariesService(seed: []));

      expect(
        find.textContaining('Aún no tienes beneficiarios'),
        findsOneWidget,
      );
      await tester.tap(find.text('Agregar beneficiario'));
      await tester.pumpAndSettle();

      expect(find.text('Nuevo Beneficiario'), findsOneWidget);
    });

    testWidgets('error muestra reintento y reintenta', (tester) async {
      final fake = FakeBeneficiariesService()
        ..listError = ApiException.network();
      await tester.pumpWidget(_harness(fake));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('Ocurrió un error al consultar el servidor'),
        findsOneWidget,
      );
      expect(find.text('Reintentar conexión'), findsOneWidget);

      fake.listError = null;
      await tester.tap(find.text('Reintentar conexión'));
      await tester.pumpAndSettle();

      expect(find.text('Mamá Lima'), findsOneWidget);
    });

    testWidgets('vista alfabetica agrupa por inicial', (tester) async {
      _tallViewport(tester);
      await _pumpList(tester, FakeBeneficiariesService());

      await tester.tap(find.text('Alfabética (A-Z)'));
      await tester.pumpAndSettle();

      // Iniciales de la semilla: Carlos(C), Lucía(L), Mamá(M), Papá(P).
      expect(find.text('C'), findsOneWidget);
      expect(find.text('L'), findsOneWidget);
      expect(find.text('M'), findsOneWidget);
      expect(find.text('P'), findsOneWidget);
      expect(find.text('Interbank · •••• 9102'), findsOneWidget);
    });

    testWidgets('eliminar exige confirmacion y borra tras 2xx', (tester) async {
      final fake = FakeBeneficiariesService();
      await _pumpList(tester, fake);

      await tester.tap(find.byKey(const Key('beneficiary-options-b2')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Eliminar'));
      await tester.pumpAndSettle();

      // Dialogo de confirmacion (mockup Alta 5).
      expect(find.text('¿Eliminar beneficiario?'), findsOneWidget);
      expect(find.text('Esta acción no se puede revertir'), findsOneWidget);

      await tester.tap(find.byKey(const Key('delete-confirm')));
      await tester.pumpAndSettle();

      expect(fake.removeCalls, 1);
      expect(fake.lastRemovedId, 'b2');
      expect(find.text('Papá Depa'), findsNothing);
      expect(find.text('Mamá Lima'), findsOneWidget);
    });

    testWidgets('cancelar el dialogo no elimina', (tester) async {
      final fake = FakeBeneficiariesService();
      await _pumpList(tester, fake);

      await tester.tap(find.byKey(const Key('beneficiary-options-b2')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Eliminar'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('delete-cancel')));
      await tester.pumpAndSettle();

      expect(fake.removeCalls, 0);
      expect(find.text('Papá Depa'), findsOneWidget);
    });
  });

  group('BeneficiaryFormPage', () {
    Future<void> pumpNew(
      WidgetTester tester,
      FakeBeneficiariesService fake,
    ) async {
      await tester.pumpWidget(_harness(fake, initial: '/beneficiaries/new'));
      await tester.pumpAndSettle();
    }

    Future<void> fillValidForm(WidgetTester tester) async {
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

    testWidgets('alta exitosa guarda con tope y vuelve', (tester) async {
      final fake = FakeBeneficiariesService();
      await pumpNew(tester, fake);
      await fillValidForm(tester);

      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(fake.lastCreated, isNotNull);
      expect(fake.lastCreated!.alias, 'Mamá Lima');
      expect(fake.lastCreated!.accountOrCci, '00112345678908');
      expect(fake.lastCreated!.preapprovedLimitMinor, 50000);
      expect(fake.lastCreated!.currency, 'PEN');
      // Vuelve a la agenda tras guardar.
      expect(find.text('Agenda de Beneficiarios'), findsOneWidget);
    });

    testWidgets('duplicado muestra banner con codigo y se ignora', (
      tester,
    ) async {
      final fake = FakeBeneficiariesService()
        ..saveError = ApiException(
          code: 'DUPLICATE_BENEFICIARY',
          message: 'Duplicado.',
        );
      await pumpNew(tester, fake);
      await fillValidForm(tester);

      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('duplicate-banner')), findsOneWidget);
      expect(find.text('REGISTRO DETECTADO'), findsOneWidget);
      expect(find.text('DUPLICATE_BENEFICIARY'), findsOneWidget);

      // El formulario pudo quedar con scroll abajo (enterText enfoca);
      // se revela el banner antes de tocarlo.
      await tester.ensureVisible(find.byKey(const Key('duplicate-dismiss')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('duplicate-dismiss')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('duplicate-banner')), findsNothing);
    });

    testWidgets('error CCI se muestra bajo el campo', (tester) async {
      final fake = FakeBeneficiariesService()
        ..saveError = ApiException(
          code: 'VALIDATION_ERROR',
          message: 'Invalido.',
        );
      await pumpNew(tester, fake);
      await fillValidForm(tester);

      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('Dígito verificador de cuenta incorrecto'),
        findsOneWidget,
      );
    });

    testWidgets('modo RUC valida 11 digitos y muestra banner SUNAT', (
      tester,
    ) async {
      final fake = FakeBeneficiariesService();
      await pumpNew(tester, fake);

      await tester.tap(find.byKey(const Key('doc-type-ruc')));
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('beneficiary-doc-number')),
        '20601234567',
      );
      await tester.tap(find.byKey(const Key('beneficiary-doc-validate')));
      await tester.pump();

      expect(find.byKey(const Key('doc-verified-banner')), findsOneWidget);
      expect(find.textContaining('SUNAT'), findsOneWidget);
    });

    testWidgets('DNI corto muestra error de formato', (tester) async {
      final fake = FakeBeneficiariesService();
      await pumpNew(tester, fake);

      await tester.enterText(
        find.byKey(const Key('beneficiary-doc-number')),
        '123',
      );
      await tester.tap(find.byKey(const Key('beneficiary-doc-validate')));
      await tester.pump();

      expect(find.text('Ingresa 8 dígitos para DNI.'), findsOneWidget);
      expect(fake.lastCreated, isNull);
    });

    testWidgets('edicion actualiza el tope (CA-03)', (tester) async {
      final fake = FakeBeneficiariesService();
      await tester.pumpWidget(_harness(fake));
      await tester.pumpAndSettle();

      // Abre edicion desde el menu de la tarjeta.
      await tester.tap(find.byKey(const Key('beneficiary-options-b1')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Editar beneficiario'));
      await tester.pumpAndSettle();

      expect(find.text('Editar Beneficiario'), findsOneWidget);
      // Cambia el tope a S/ 300 (la tarjeta puede estar bajo el pliegue).
      await tester.ensureVisible(find.byKey(const Key('tope-300')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('tope-300')));
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('beneficiary-doc-number')),
        '42891044',
      );
      await tester.enterText(
        find.byKey(const Key('beneficiary-account')),
        '00112345678908',
      );
      await tester.tap(find.byKey(const Key('beneficiary-save')));
      await tester.pumpAndSettle();

      expect(fake.lastUpdatedId, 'b1');
      expect(fake.lastUpdated!.preapprovedLimitMinor, 30000);
    });

    testWidgets('el CCI completo nunca se muestra en pantalla', (tester) async {
      final fake = FakeBeneficiariesService();
      await _pumpList(tester, fake);

      // La lista solo expone enmascarados.
      expect(find.textContaining('00112345678908'), findsNothing);
      expect(find.text('•••• 7744 · Cuenta Soles'), findsOneWidget);
    });
  });

  group('BeneficiariesService (HTTP)', () {
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

    const listBody = {
      'data': [
        {
          'id': 'b1',
          'alias': 'Mamá Lima',
          'photo_url': null,
          'bank_code': 'BCP',
          'account_masked': '•••• 7744',
          'holder_name': 'M. Rojas ••••',
          'preapproved_limit_minor': 50000,
          'currency': 'PEN',
          'status': 'ACTIVE',
        },
      ],
      'meta': {'total': 1, 'q': 'mama'},
    };

    test('list envia ?q= y parsea enmascarados', () async {
      Map<String, dynamic>? capturedQuery;
      final api = stubClient(
        (o) => Response(requestOptions: o, statusCode: 200, data: listBody),
        onRequest: (o) {
          capturedQuery = Map<String, dynamic>.from(o.queryParameters);
          return null;
        },
      );

      final items = await BeneficiariesService(api: api).list(query: 'mama');

      expect(capturedQuery, containsPair('q', 'mama'));
      expect(items, hasLength(1));
      expect(items.single.accountMasked, '•••• 7744');
      expect(items.single.preapprovedLimitMinor, 50000);
    });

    test(
      'create usa Idempotency-Key y no expone cuenta en respuesta',
      () async {
        String? idempotencyKey;
        Map<String, dynamic>? capturedBody;
        final api = stubClient(
          (o) => Response(
            requestOptions: o,
            statusCode: 201,
            data: {
              'data': {
                'id': 'b-new',
                'alias': 'Mamá Lima',
                'bank_code': 'BCP',
                'account_masked': '•••• 0012',
                'holder_name': '••••',
                'currency': 'PEN',
                'status': 'ACTIVE',
              },
              'meta': {'replayed': false},
            },
          ),
          onRequest: (o) {
            idempotencyKey = o.headers['Idempotency-Key']?.toString();
            if (o.data is Map) {
              capturedBody = Map<String, dynamic>.from(o.data as Map);
            }
            return null;
          },
        );

        final created = await BeneficiariesService(api: api).create(
          const BeneficiaryInput(
            alias: 'Mamá Lima',
            accountOrCci: '00112345678908',
            bankCode: 'BCP',
            preapprovedLimitMinor: 50000,
          ),
        );

        expect(idempotencyKey, isNotNull);
        expect(idempotencyKey, isNotEmpty);
        expect(capturedBody!['account_or_cci'], '00112345678908');
        expect(created.accountMasked, '•••• 0012');
      },
    );

    test('remove exige confirm=true', () async {
      Map<String, dynamic>? capturedQuery;
      String? capturedPath;
      final api = stubClient(
        (o) => Response(
          requestOptions: o,
          statusCode: 200,
          data: {
            'data': {
              'id': 'b1',
              'alias': 'Mamá Lima',
              'account_masked': '•••• 7744',
              'status': 'DELETED',
            },
            'meta': {},
          },
        ),
        onRequest: (o) {
          capturedPath = o.path;
          capturedQuery = Map<String, dynamic>.from(o.queryParameters);
          return null;
        },
      );

      final deleted = await BeneficiariesService(api: api).remove('b1');

      expect(capturedPath, contains('/beneficiaries/b1'));
      expect(capturedQuery, containsPair('confirm', 'true'));
      expect(deleted.status, 'DELETED');
    });

    test('duplicado mapea DUPLICATE_BENEFICIARY', () async {
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
      final api = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 'token'),
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) => handler.reject(
            DioException(
              requestOptions: options,
              response: Response(
                requestOptions: options,
                statusCode: 409,
                data: {
                  'error': {
                    'code': 'DUPLICATE_BENEFICIARY',
                    'message': 'Duplicado.',
                  },
                },
              ),
              type: DioExceptionType.badResponse,
            ),
          ),
        ),
      );

      try {
        await BeneficiariesService(
          api: api,
        ).create(const BeneficiaryInput(alias: 'X', accountOrCci: '12345678'));
        fail('debió lanzar ApiException');
      } on ApiException catch (e) {
        expect(e.code, 'DUPLICATE_BENEFICIARY');
      }
    });
  });
}
