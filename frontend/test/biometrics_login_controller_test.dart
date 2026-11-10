// Pruebas del orquestador challenge -> biometric -> sign -> facial (F-T03).
//
// - Biometria disponible (fake) -> firma con formato correcto enviada al facial.
// - Biometria no disponible -> fallback PIN senalado, facial NUNCA llamado.
// - Firma con formato invalido -> detectada en cliente, facial NUNCA llamado.
import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/secure_key_value_storage.dart';
import 'package:banca_online/core/session/secure_session_repository.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/biometrics/biometric_service.dart';
import 'package:banca_online/features/biometrics/login_controller.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:uuid/uuid.dart';

/// `BiometricService` que emite una firma con formato invalido a proposito.
class _BadSigner extends BiometricService {
  _BadSigner({required super.session, required super.reader});

  @override
  Future<String> signNonce(String nonce) async => 'no-es-una-firma-valida';
}

class _HttpMock {
  _HttpMock() {
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          if (options.path.endsWith(LoginController.challengePath)) {
            challengeCalls++;
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {'nonce': 'n-abc-123', 'expires_in': 120},
                  'meta': {'request_id': 'r-challenge'},
                },
              ),
            );
            return;
          }
          if (options.path.endsWith(LoginController.facialPath)) {
            facialCalls++;
            lastFacialPayload = Map<String, dynamic>.from(
              options.data as Map,
            );
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: {
                  'data': {
                    'access_token': 'acc-1',
                    'refresh_token': 'ref-1',
                    'token_type': 'Bearer',
                    'session_id': 'ses-1',
                    'expires_in': 900,
                  },
                  'meta': {'request_id': 'r-facial'},
                },
              ),
            );
            return;
          }
          handler.next(options);
        },
      ),
    );
  }

  final Dio dio = Dio();
  int challengeCalls = 0;
  int facialCalls = 0;
  Map<String, dynamic>? lastFacialPayload;
}

LoginController _controllerWith({
  required SecureSessionRepository session,
  required BiometricService biometrics,
  required _HttpMock http,
}) {
  final api = ApiClient.create(
    session: session,
    dioOverride: http.dio,
    refreshDioOverride: Dio(),
    uuid: const Uuid(),
  );
  return LoginController(api: api, biometrics: biometrics);
}

void main() {
  test('biometria disponible: firma con formato correcto enviada al facial',
      () async {
    final storage = InMemorySecureStorage();
    final session = SecureSessionRepository(storage: storage);
    final reader = FakeBiometricReader(available: true, succeeds: true);
    final biometrics = BiometricService(session: session, reader: reader);
    final http = _HttpMock();
    final controller = _controllerWith(
      session: session,
      biometrics: biometrics,
      http: http,
    );
    addTearDown(controller.dispose);

    await controller.loginWithBiometrics(
      userRef: 'user-1',
      deviceId: 'pixel-8-pro',
      reason: 'Confirma tu identidad para ingresar',
    );

    expect(controller.succeeded, isTrue);
    expect(controller.pinFallbackRequired, isFalse);
    expect(controller.errorMessage, isNull);
    expect(http.challengeCalls, 1);
    expect(http.facialCalls, 1);

    // La firma enviada es HMAC-SHA256 del nonce con el device.secret.
    final payload = http.lastFacialPayload!;
    expect(payload['nonce'], 'n-abc-123');
    expect(payload['device_id'], 'pixel-8-pro');
    final sent = payload['signature'] as String;
    expect(BiometricService.isValidSignatureFormat(sent), isTrue);
    expect(sent, await biometrics.signNonce('n-abc-123'));

    expect(reader.authenticateCalls, 1);
    expect(
      controller.sessionResult?['token_type'],
      'Bearer',
    );
  });

  test('biometria no disponible: fallback PIN y facial nunca llamado',
      () async {
    final storage = InMemorySecureStorage();
    final session = SecureSessionRepository(storage: storage);
    final http = _HttpMock();
    final controller = _controllerWith(
      session: session,
      biometrics: BiometricService(
        session: session,
        reader: FakeBiometricReader(available: false),
      ),
      http: http,
    );
    addTearDown(controller.dispose);

    await controller.loginWithBiometrics(
      userRef: 'user-1',
      deviceId: 'pixel-8-pro',
      reason: 'Confirma tu identidad para ingresar',
    );

    // El challenge si se pidio (el nonce existe), pero sin biometrico no hay
    // firma ni llamada al facial: se senala el PIN (lo monta E1-T16).
    expect(http.challengeCalls, 1);
    expect(http.facialCalls, 0);
    expect(controller.succeeded, isFalse);
    expect(controller.pinFallbackRequired, isTrue);
    expect(controller.errorMessage, contains('PIN'));
  });

  test('biometria cancelada por el usuario: tambien cae a fallback PIN',
      () async {
    final storage = InMemorySecureStorage();
    final session = SecureSessionRepository(storage: storage);
    final http = _HttpMock();
    final controller = _controllerWith(
      session: session,
      biometrics: BiometricService(
        session: session,
        reader: FakeBiometricReader(available: true, succeeds: false),
      ),
      http: http,
    );
    addTearDown(controller.dispose);

    await controller.loginWithBiometrics(
      userRef: 'user-1',
      deviceId: 'pixel-8-pro',
      reason: 'Confirma tu identidad para ingresar',
    );

    expect(http.facialCalls, 0);
    expect(controller.pinFallbackRequired, isTrue);
  });

  test('firma con formato invalido: detectada en cliente, no se envia',
      () async {
    final storage = InMemorySecureStorage();
    final session = SecureSessionRepository(storage: storage);
    final http = _HttpMock();
    final controller = _controllerWith(
      session: session,
      biometrics: _BadSigner(
        session: session,
        reader: FakeBiometricReader(),
      ),
      http: http,
    );
    addTearDown(controller.dispose);

    await controller.loginWithBiometrics(
      userRef: 'user-1',
      deviceId: 'pixel-8-pro',
      reason: 'Confirma tu identidad para ingresar',
    );

    expect(http.challengeCalls, 1);
    expect(http.facialCalls, 0,
        reason: 'formato invalido no debe salir a red');
    expect(controller.succeeded, isFalse);
    expect(controller.pinFallbackRequired, isFalse);
    expect(controller.state, BiometricLoginState.error);
    expect(controller.errorMessage, contains('formato'));
  });
}
