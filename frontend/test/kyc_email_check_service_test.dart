// Pruebas del prechequeo de email (F-T50, E1-T40).
//
// - `checkEmail` envia `{email}` a `/auth/kyc/email/check`.
// - `200` no lanza (disponible); `409 DUPLICATE_EMAIL` se propaga como
//   `ApiException` con ese codigo (mensaje del catalogo, sin PII en logs).
import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/core/errors/error_messages.dart';
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

Dio _bareDio() => Dio(BaseOptions(baseUrl: 'http://localhost'));

HttpKycService _service(Dio dio) => HttpKycService(
      ApiClient.create(
        session: InMemorySessionRepository(),
        baseUrl: 'http://localhost/api/v1',
        dioOverride: dio,
      ),
    );

void main() {
  test('DUPLICATE_EMAIL existe en el catalogo (no cae al generico)', () {
    expect(apiErrorMessagesEs.containsKey('DUPLICATE_EMAIL'), isTrue);
    expect(
      messageForCode('DUPLICATE_EMAIL'),
      'El correo ya existe.',
    );
    expect(
      messageForCode('DUPLICATE_EMAIL'),
      isNot(messageForCode('UNKNOWN')),
    );
  });

  test('checkEmail envia {email} a /auth/kyc/email/check y pasa en 200',
      () async {
    final dio = _bareDio();
    final service = _service(dio);
    RequestOptions? captured;
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          captured = options;
          handler.resolve(
            Response(
              requestOptions: options,
              statusCode: 200,
              data: {
                'data': {'available': true},
                'meta': {'request_id': 'r-1'},
              },
            ),
          );
        },
      ),
    );

    await service.checkEmail(email: 'nuevo@banco.com');

    expect(captured!.path, HttpKycService.emailCheckPath);
    expect(captured!.path, '/auth/kyc/email/check');
    final data = captured!.data as Map<String, dynamic>;
    expect(data, {'email': 'nuevo@banco.com'});
  });

  test('checkEmail propaga DUPLICATE_EMAIL en 409', () async {
    final dio = _bareDio();
    final service = _service(dio);
    RequestOptions? captured;
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          captured = options;
          handler.reject(
            DioException(
              requestOptions: options,
              type: DioExceptionType.badResponse,
              response: Response(
                requestOptions: options,
                statusCode: 409,
                data: {
                  'error': {
                    'code': 'DUPLICATE_EMAIL',
                    'message': 'interno',
                  },
                },
              ),
            ),
          );
        },
      ),
    );

    expect(
      service.checkEmail(email: 'existe@banco.com'),
      throwsA(
        isA<ApiException>()
            .having((e) => e.code, 'code', 'DUPLICATE_EMAIL')
            .having(
              (e) => e.message,
              'message',
              'El correo ya existe.',
            ),
      ),
    );
    // La ruta/cuerpo se verifican al rechazar (el interceptor capturo).
    await expectLater(
      service.checkEmail(email: 'existe@banco.com'),
      throwsA(isA<ApiException>()),
    );
    expect(captured!.path, HttpKycService.emailCheckPath);
    expect(
      (captured!.data as Map)['email'],
      'existe@banco.com',
    );
  });
}
