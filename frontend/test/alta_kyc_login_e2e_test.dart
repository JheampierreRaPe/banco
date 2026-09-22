// E2E de navegacion del alta y login (Q-T10; HU01/HU02/HU03).
//
// Recorre con las PANTALLAS REALES (widget test) el flujo F-T39:
//   KYC exitoso -> crear PIN -> confirmar PIN -> biometrico (omitir) ->
//   OTP (`POST /auth/pin/setup`) -> success (guarda `user_ref`) -> /login
// y verifica que el login con PIN envia `device_public_key` ("hmac:<hex>").
//
// Cero red real: el KYC usa un `KycService` falso, el setup de PIN y el
// reenvio usan fakes y el HTTP del login se intercepta con un `Dio` en
// memoria. El `device_public_key` se deriva del `SessionRepository` en
// memoria.
//
// Regresion SCR-005: el `user_ref` NO se persiste en el resultado KYC ni en
// los pasos previos; SOLO en la pantalla de registro exitoso.
import 'dart:typed_data';

import 'package:banca_online/core/http/api_client.dart';
import 'package:banca_online/core/session/in_memory_session_repository.dart';
import 'package:banca_online/core/session/session_identity_store.dart';
import 'package:banca_online/features/activation/activation_service.dart';
import 'package:banca_online/features/biometrics/biometric_offer_page.dart';
import 'package:banca_online/features/biometrics/biometric_reader.dart';
import 'package:banca_online/features/biometrics/biometric_service.dart';
import 'package:banca_online/features/biometrics/login_controller.dart' as bio;
import 'package:banca_online/features/kyc/kyc_flow_controller.dart';
import 'package:banca_online/features/kyc/kyc_models.dart';
import 'package:banca_online/features/kyc/kyc_service.dart';
import 'package:banca_online/features/kyc/presentation/kyc_result_page.dart';
import 'package:banca_online/features/login/login_controller.dart';
import 'package:banca_online/features/login/login_page.dart';
import 'package:banca_online/features/pin_setup/pin_confirm_page.dart';
import 'package:banca_online/features/pin_setup/pin_create_page.dart';
import 'package:banca_online/features/pin_setup/pin_setup_otp_page.dart';
import 'package:banca_online/features/pin_setup/pin_setup_service.dart';
import 'package:banca_online/features/pin_setup/registration_success_page.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

const String kUserId = 'u-42';
const String kDeviceId = 'device-e2e';
const String kPin = '482916';
const String kCode = '123456';

Map<String, dynamic> _sessionEnvelope() => {
      'data': {
        'access_token': 'acc-pin-e2e',
        'refresh_token': 'ref-pin-e2e',
        'token_type': 'Bearer',
        'session_id': 'ses-e2e',
        'expires_in': 900,
      },
      'meta': {'request_id': 'r-e2e'},
    };

/// KYC falso: exito determinista con `user_id` para el alta (sin frames/red).
class _FakeKycService implements KycService {
  @override
  Future<KycChallenge> challenge() async => const KycChallenge(
        token: 'tok-e2e',
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
        overallResult: true,
        detailCode: 'OK',
        userId: kUserId,
        status: 'ONBOARDED',
        accountId: 'acc-1',
      );
}

/// Fake del setup de PIN: exito sin red.
class _FakePinSetupService implements PinSetupService {
  int calls = 0;
  Map<String, String> lastArgs = {};

  @override
  Future<PinSetupResult> setup({
    required String userRef,
    required String code,
    required String pin,
  }) async {
    calls++;
    lastArgs = {'userRef': userRef, 'code': code, 'pin': pin};
    return const PinSetupResult(userId: kUserId, status: 'ACTIVE');
  }
}

/// Fake del reenvio (mismo contrato de `activation`).
class _FakeResendService implements ActivationService {
  @override
  Future<ActivationResult> activate({
    required String userRef,
    required String code,
  }) =>
      throw UnimplementedError();

  @override
  Future<ResendResult> resend({required String userRef, String? channel}) async =>
      const ResendResult(userId: kUserId, resendCount: 1, expiresInSeconds: 600);
}

/// Intercepta `POST /auth/login/pin` (sin red) y captura el payload enviado.
class _LoginHttpMock {
  _LoginHttpMock() {
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          if (options.path.endsWith(LoginController.pinPath)) {
            pinCalls++;
            lastPinPayload = Map<String, dynamic>.from(options.data as Map);
            handler.resolve(
              Response(
                requestOptions: options,
                statusCode: 200,
                data: _sessionEnvelope(),
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
  int pinCalls = 0;
  Map<String, dynamic>? lastPinPayload;
}

/// Marca digitos en el teclado propio del fig.
Future<void> _enterDigits(WidgetTester tester, String digits) async {
  for (final digit in digits.split('')) {
    final key = find.byKey(Key('pin-key-$digit'));
    await tester.ensureVisible(key);
    await tester.tap(key);
    await tester.pump();
  }
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
      'alta KYC -> PIN -> OTP -> success (guarda user_ref) -> login PIN',
      (tester) async {
    final session = InMemorySessionRepository();
    final identity = InMemorySessionIdentityStore(deviceId: kDeviceId);
    final http = _LoginHttpMock();

    final api = ApiClient.create(
      session: session,
      dioOverride: http.dio,
      refreshDioOverride: Dio(),
      uuid: const Uuid(),
    );

    final kycController = KycFlowController(service: _FakeKycService());
    addTearDown(kycController.dispose);

    // Deja el resultado KYC en exito (mismo estado que tras enviar el submit).
    await kycController.loadChallenge();
    kycController.setDocument(type: 'DNI', number: '12345678');
    kycController.setApplicant(
      const KycApplicant(
        firstName: 'Ana',
        lastName: 'Perez',
        email: 'ana@example.com',
      ),
    );
    await kycController.captureAndResolveCurrentTask();
    await kycController.captureAndResolveCurrentTask();
    await kycController.submit();
    expect(kycController.result?.overallResult, isTrue);

    final pinSetup = _FakePinSetupService();
    final resend = _FakeResendService();
    final loginController = LoginController(
      api: api,
      session: session,
      biometricLogin: bio.LoginController(
        api: api,
        biometrics: BiometricService(session: session, reader: FakeBiometricReader()),
      ),
      identity: identity,
    );
    addTearDown(loginController.dispose);

    final router = GoRouter(
      initialLocation: '/kyc/result',
      routes: [
        GoRoute(
          path: '/kyc/result',
          builder: (context, state) => KycResultPage(
            controller: kycController,
            identity: identity,
          ),
        ),
        GoRoute(
          path: '/pin-setup',
          builder: (context, state) => PinCreatePage(
            userRef: state.uri.queryParameters['userRef'] ?? '',
          ),
        ),
        GoRoute(
          path: '/pin-setup/confirm',
          builder: (context, state) => PinConfirmPage(
            userRef: state.uri.queryParameters['userRef'] ?? '',
            pin: state.extra is String ? state.extra as String : '',
          ),
        ),
        GoRoute(
          path: '/pin-setup/biometrics',
          builder: (context, state) => BiometricOfferPage(
            userRef: state.uri.queryParameters['userRef'] ?? '',
            pin: state.extra is String ? state.extra as String : '',
            reader: FakeBiometricReader(available: false),
          ),
        ),
        GoRoute(
          path: '/pin-setup/otp',
          builder: (context, state) => PinSetupOtpPage(
            userRef: state.uri.queryParameters['userRef'] ?? '',
            pin: state.extra is String ? state.extra as String : '',
            setupService: pinSetup,
            resendService: resend,
            identity: identity,
          ),
        ),
        GoRoute(
          path: '/registration-success',
          builder: (context, state) => RegistrationSuccessPage(
            userRef: state.uri.queryParameters['userRef'] ?? '',
            identity: identity,
          ),
        ),
        GoRoute(
          path: '/login',
          builder: (context, state) => LoginPage(
            controller: loginController,
            userRef: state.uri.queryParameters['userRef'] ?? '',
            deviceId: state.uri.queryParameters['deviceId'] ?? '',
            autoTick: false,
          ),
        ),
        GoRoute(
          path: '/home',
          builder: (context, state) => const Scaffold(body: Text('home-ok')),
        ),
      ],
    );
    addTearDown(router.dispose);

    await tester.pumpWidget(MaterialApp.router(routerConfig: router));
    await tester.pumpAndSettle();

    // Paso 1: KYC exitoso -> continuar navega a crear PIN SIN guardar
    // el `user_ref` (guardado tardio F-T39).
    expect(find.text('Verificacion exitosa'), findsOneWidget);
    await tester.tap(find.byKey(const Key('kyc-result-continue')));
    await tester.pumpAndSettle();
    expect(identity.userRef, isNull);
    expect(find.text('Crea tu PIN de seguridad'), findsOneWidget);

    // Paso 2: crear PIN -> confirmar (el PIN viaja en memoria).
    await _enterDigits(tester, kPin);
    expect(find.text('Confírmalo'), findsOneWidget);
    expect(identity.userRef, isNull);
    await _enterDigits(tester, kPin);

    // Paso 3: oferta biometrica (omitir: no bloquea, fallback a PIN).
    expect(find.text('¿Quieres entrar con tu huella?'), findsOneWidget);
    expect(identity.userRef, isNull);
    await tester.tap(find.byKey(const Key('biometric-skip')));
    await tester.pumpAndSettle();

    // Paso 4: OTP por email -> `POST /auth/pin/setup`.
    expect(find.text('Revisa tu correo'), findsOneWidget);
    await _enterDigits(tester, kCode);
    await tester.tap(find.byKey(const Key('pin-setup-submit')));
    await tester.pumpAndSettle();

    expect(pinSetup.calls, 1);
    expect(pinSetup.lastArgs['userRef'], kUserId);
    expect(pinSetup.lastArgs['code'], kCode);
    expect(pinSetup.lastArgs['pin'], kPin);

    // Paso 5: success guarda el `user_ref` (UNICO lugar) y va al login.
    expect(find.text('¡Tu cuenta está activa!'), findsOneWidget);
    expect(identity.userRef, kUserId);
    await tester.tap(find.byKey(const Key('registration-success-login')));
    await tester.pumpAndSettle();
    expect(find.text('Inicia sesión'), findsOneWidget);

    // Paso 6: login con PIN (6 casillas canónicas F-T40); el payload viaja
    // con userRef/deviceId + HMAC.
    for (var i = 0; i < 6; i++) {
      await tester.enterText(find.byKey(Key('login-pin-$i')), kPin[i]);
      await tester.pump();
    }
    await tester.pump();
    await tester.tap(find.byKey(const Key('login-pin-submit')));
    await tester.pumpAndSettle();

    expect(find.text('home-ok'), findsOneWidget);
    expect(http.pinCalls, 1);
    final payload = http.lastPinPayload!;
    expect(payload['user_ref'], kUserId);
    expect(payload['device_id'], kDeviceId);
    expect(payload['pin'], kPin);
    expect(
      payload['device_public_key'],
      matches(RegExp(r'^hmac:[0-9a-f]{64}$')),
    );
    expect(session.isAuthenticated, isTrue);
  });
}
