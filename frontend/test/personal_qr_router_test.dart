import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/router/app_router.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/wallet/data/personal_qr_service.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Regresión ALTO-1: la fábrica del QR personal queda cableada por el
/// orquestador real (`buildRouter`, nunca inyectando `service:` a mano)
/// y la ruta `/wallet/personal-qr` genera el QR de verdad.
void main() {
  testWidgets(
      'router real cablea personalQrServiceFactory y genera el QR personal',
      (tester) async {
    final session = InMemorySessionRepository(initialAccessToken: 'token');
    final identity = InMemorySessionIdentityStore();
    addTearDown(identity.dispose);

    final dio = Dio(BaseOptions(baseUrl: 'http://localhost/api/v1'));
    final api = ApiClient.create(session: session, dioOverride: dio);
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          if (options.path.endsWith('/wallet/qr')) {
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 201,
                data: {
                  'data': {
                    'charge_id': 'chg-router-1',
                    'qr_content': 'BO-QR1.router-cableado',
                    'payload_hash': 'h',
                    'currency': 'PEN',
                    'mode': 'STATIC',
                    'status': 'ACTIVE',
                    'owner_user_id': 'owner-1',
                    'reusable': true,
                  },
                },
              ),
            );
            return;
          }
          handler.next(options);
        },
      ),
    );

    final router = buildRouter(
      session,
      identity: identity,
      api: api,
      initialLocation: '/wallet/personal-qr',
    );
    addTearDown(router.dispose);

    // La fábrica queda cableada por el orquestador (sin este cableado
    // era `null` y la página mostraba "no configurado").
    expect(personalQrServiceFactory, isNotNull);
    expect(personalQrServiceFactory!(), isA<PersonalQrService>());

    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    await tester.pumpAndSettle();

    // La ruta real monta la página con servicio no-nulo: genera el QR.
    expect(find.byKey(const Key('personal-qr-form')), findsOneWidget);
    await tester.tap(find.byKey(const Key('personal-qr-generate')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('personal-qr-result')), findsOneWidget);
    expect(
      find.textContaining('Servicio de QR personal no configurado'),
      findsNothing,
    );
  });
}
