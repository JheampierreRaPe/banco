import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/credits/data/credit_contract_service.dart';
import 'package:banca_online/features/credits/models/credit_contract.dart';
import 'package:banca_online/features/credits/models/loan_application.dart';
import 'package:banca_online/features/credits/presentation/credit_contract_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Regresión del defecto "No encontramos el contrato" (get-or-create).
///
/// Causa: la pantalla solo hacía `GET .../contract`, pero el juego se crea
/// con `POST .../contract` y ningún cliente lo invocaba: sin juego previo
/// el `GET` da 404 y la pantalla mostraba el error. Fix: ante 404 la
/// pantalla genera el juego (`generateContract`, idempotente) y lo muestra.
///
/// Sin el fix, el test "404 genera y muestra" falla (muestra el error,
/// nunca llama a `generateContract` y no hay contenido).
class FakeGetOrCreateService implements CreditContractServiceBase {
  ContractBundle bundle = _bundle();
  ApiException? getError;
  ApiException? generateError;

  int getCalls = 0;
  int generateCalls = 0;
  String? lastGenerateKey;

  static ContractBundle _bundle() {
    return ContractBundle.fromJson({
      'application_id': 'app-1',
      'status': 'GENERATED',
      'documents': [
        {
          'contract_id': 'c-1',
          'doc_type': 'CONTRATO',
          'doc': 'contrato',
          'doc_ref': 'ref-1',
          'doc_hash': 'hash-1',
          'status': 'GENERATED',
        },
        {
          'contract_id': 'c-2',
          'doc_type': 'PAGARE',
          'doc': 'pagare',
          'doc_ref': 'ref-2',
          'doc_hash': 'hash-2',
          'status': 'GENERATED',
        },
        {
          'contract_id': 'c-3',
          'doc_type': 'RESUMEN',
          'doc': 'resumen',
          'doc_ref': 'ref-3',
          'doc_hash': 'hash-3',
          'status': 'GENERATED',
        },
      ],
    });
  }

  @override
  Future<ContractBundle> getContract(String applicationId) async {
    getCalls++;
    final error = getError;
    if (error != null) throw error;
    return bundle;
  }

  @override
  Future<ContractBundle> generateContract(
    String applicationId, {
    String? idempotencyKey,
  }) async {
    generateCalls++;
    lastGenerateKey = idempotencyKey;
    final error = generateError;
    if (error != null) throw error;
    return bundle;
  }

  @override
  Future<ContractPdf> downloadDocument(
    String applicationId,
    String doc,
  ) async =>
      ContractPdf(bytes: const [1, 2, 3], suggestedFilename: '$doc.pdf');

  @override
  Future<SignChallenge> requestChallenge(
    String applicationId, {
    String? deviceId,
  }) async =>
      const SignChallenge(
        applicationId: 'app-1',
        nonce: 'nonce-1',
        expiresIn: 120,
        status: 'GENERATED',
      );

  @override
  Future<ContractSignatureResult> signWithBiometrics({
    required String applicationId,
    required String nonce,
    required String signature,
    required String deviceId,
    String? idempotencyKey,
  }) async =>
      const ContractSignatureResult(
        applicationId: 'app-1',
        contractId: 'c-1',
        status: 'SIGNED',
      );

  @override
  Future<ContractSignatureResult> signWithPin({
    required String applicationId,
    required String pin,
    String? idempotencyKey,
  }) async =>
      const ContractSignatureResult(
        applicationId: 'app-1',
        contractId: 'c-1',
        status: 'SIGNED',
      );
}

const _approvedApplication = LoanApplicationResult(
  id: 'app-1',
  status: LoanApplicationStatus.approved,
  decisionReason: 'Score y capacidad de pago dentro de política.',
  requestedAmountMinor: 1000000,
  termMonths: 12,
  totalIncomeMinor: 350000,
  totalDebtMinor: 0,
);

ApiException _notFound() => ApiException(
      code: 'NOT_FOUND',
      message: 'No encontramos lo que buscas.',
      statusCode: 404,
    );

Future<void> _pump(
  WidgetTester tester,
  FakeGetOrCreateService fake, {
  LoanApplicationResult? application,
  String? applicationId,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: CreditContractPage(
        service: fake,
        application: application,
        applicationId: applicationId,
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  group('CreditContract get-or-create', () {
    testWidgets('GET 404 genera el juego y muestra los 3 documentos',
        (tester) async {
      final fake = FakeGetOrCreateService()..getError = _notFound();
      await _pump(tester, fake, application: _approvedApplication);

      expect(fake.getCalls, 1);
      expect(fake.generateCalls, 1);
      expect(find.byKey(const Key('contract-content')), findsOneWidget);
      expect(find.byKey(const Key('contract-doc-0')), findsOneWidget);
      expect(find.byKey(const Key('contract-doc-1')), findsOneWidget);
      expect(find.byKey(const Key('contract-doc-2')), findsOneWidget);
      expect(find.byKey(const Key('contract-error')), findsNothing);
    });

    testWidgets('GET 200 no regenera (sin POST duplicado)', (tester) async {
      final fake = FakeGetOrCreateService();
      await _pump(tester, fake, application: _approvedApplication);

      expect(fake.getCalls, 1);
      expect(fake.generateCalls, 0);
      expect(find.byKey(const Key('contract-content')), findsOneWidget);
    });

    testWidgets('409 en la generación: mensaje claro, sin contenido',
        (tester) async {
      final fake = FakeGetOrCreateService()
        ..getError = _notFound()
        ..generateError = ApiException(
          code: 'CONFLICT',
          message: 'Conflicto.',
          serverMessage:
              'el expediente en estado PENDING no admite generacion de contrato',
          statusCode: 409,
        );
      await _pump(tester, fake, application: _approvedApplication);

      expect(fake.generateCalls, 1);
      expect(find.byKey(const Key('contract-content')), findsNothing);
      expect(
        find.text(
          'El contrato se genera cuando el crédito está aprobado. '
          'Vuelve al estado para verificar la aprobación.',
        ),
        findsOneWidget,
      );
    });

    testWidgets('flujo Mis créditos (solo applicationId): 404 genera',
        (tester) async {
      // "Mis créditos" navega con `extra: item.id` (String).
      final fake = FakeGetOrCreateService()..getError = _notFound();
      await _pump(tester, fake, applicationId: 'app-1');

      expect(fake.getCalls, 1);
      expect(fake.generateCalls, 1);
      expect(find.byKey(const Key('contract-content')), findsOneWidget);
    });

    testWidgets('flujo Estado (application): 404 genera y hero del servidor',
        (tester) async {
      // "Estado" navega con `extra: result` (LoanApplicationResult).
      final fake = FakeGetOrCreateService()..getError = _notFound();
      await _pump(tester, fake, application: _approvedApplication);

      expect(fake.generateCalls, 1);
      expect(find.byKey(const Key('contract-content')), findsOneWidget);
      expect(find.text('S/ 10,000.00 • 12 meses'), findsOneWidget);
    });

    test('mensajes: firmado se preserva, 401/red accionables', () {
      // 409 de firma (sin pista de generación) sigue diciendo ya firmado.
      expect(
        creditContractUserMessage(
          ApiException(code: 'CONFLICT', message: 'x', statusCode: 409),
        ),
        'Este contrato ya fue firmado. Revisa tu correo.',
      );
      expect(
        creditContractUserMessage(
          ApiException(
            code: 'NOT_AUTHENTICATED',
            message: 'x',
            statusCode: 401,
          ),
        ),
        contains('iniciar sesión'),
      );
      expect(
        creditContractUserMessage(ApiException.network()),
        isNotEmpty,
      );
    });

    test('generateContract hace POST con Idempotency-Key y parsea data',
        () async {
      RequestOptions? captured;
      String? capturedKey;
      final dio = Dio(BaseOptions(baseUrl: 'http://localhost'));
      final client = ApiClient.create(
        session: InMemorySessionRepository(initialAccessToken: 't'),
        baseUrl: 'http://localhost/api/v1',
        dioOverride: dio,
      );
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (options, handler) {
            captured = options;
            capturedKey = options.headers['Idempotency-Key']?.toString();
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 201,
                data: {
                  'data': {
                    'application_id': 'app-1',
                    'status': 'GENERATED',
                    'documents': [
                      {
                        'contract_id': 'c-1',
                        'doc_type': 'CONTRATO',
                        'doc': 'contrato',
                        'doc_ref': 'ref-1',
                        'doc_hash': 'hash-1',
                        'status': 'GENERATED',
                      },
                      {
                        'contract_id': 'c-2',
                        'doc_type': 'PAGARE',
                        'doc': 'pagare',
                        'doc_ref': 'ref-2',
                        'doc_hash': 'hash-2',
                        'status': 'GENERATED',
                      },
                      {
                        'contract_id': 'c-3',
                        'doc_type': 'RESUMEN',
                        'doc': 'resumen',
                        'doc_ref': 'ref-3',
                        'doc_hash': 'hash-3',
                        'status': 'GENERATED',
                      },
                    ],
                  },
                  'meta': {'replayed': false},
                },
              ),
            );
          },
        ),
      );

      final service = CreditContractService(api: client);
      final bundle = await service.generateContract('app-1');

      expect(bundle.documents, hasLength(3));
      expect(bundle.applicationId, 'app-1');
      expect(captured!.method, 'POST');
      expect(
        captured!.path,
        contains('/credits/applications/app-1/contract'),
      );
      expect(capturedKey, isNotNull);

      // Reintento con la misma clave la reutiliza (idempotencia).
      await service.generateContract('app-1', idempotencyKey: 'key-1');
      expect(captured!.headers['Idempotency-Key'], 'key-1');
    });
  });
}
