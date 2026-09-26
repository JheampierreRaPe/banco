import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/session/session_identity_store.dart';
import '../../core/widgets/empty_view.dart';
import '../../core/widgets/error_view.dart';
import '../activation/activation_service.dart';
import '../biometrics/biometric_offer_page.dart';
import '../biometrics/biometric_reader.dart';
import 'pin_confirm_page.dart';
import 'pin_create_page.dart';
import 'pin_setup_draft.dart';
import 'pin_setup_otp_page.dart';
import 'pin_setup_service.dart';
import 'registration_success_page.dart';

/// Fabrica del servicio de creacion de PIN usado por el paso OTP.
///
/// El arranque de la app debe asignarla con dependencias reales ([ApiClient])
/// e importar `...pinSetupRoutes` en el orquestador
/// (`core/router/app_router.dart`). En tests se asignan fakes.
typedef PinSetupServiceFactory = PinSetupService Function();

PinSetupServiceFactory? pinSetupServiceFactory;

/// Fabrica del servicio de reenvio (MISMO contrato de `activation`:
/// `POST /auth/otp/resend`). No se duplica el endpoint: se reutiliza
/// [ActivationService].
typedef PinSetupResendServiceFactory = ActivationService Function();

PinSetupResendServiceFactory? pinSetupResendServiceFactory;

/// Fabrica del lector biometrico de la oferta (F-T46).
///
/// Produccion: `null` y la pagina usa el [SystemBiometricReader] real
/// (`local_auth`). En widget tests se inyecta un [FakeBiometricReader]: sin
/// plataforma no hay prompt del SO y el plugin real nunca resuelve (el gate
/// unitario con estados se cubre en `biometric_reader_test.dart`).
typedef PinSetupBiometricReaderFactory = BiometricReader Function();

PinSetupBiometricReaderFactory? pinSetupBiometricReaderFactory;

/// Rutas del cierre del registro (F-T39, orden PIN -> OTP -> success).
///
/// - `/pin-setup?userRef=<id>`: crear PIN (fig `0:497`).
/// - `/pin-setup/confirm?userRef=<id>` (+ `extra` = [PinSetupDraft] en
///   memoria): confirmar PIN (fig `0:604`).
/// - `/pin-setup/biometrics?userRef=<id>` (+ `extra` = [PinSetupDraft]):
///   oferta biometrica, opcional y sin bloqueo (fig `0:704`).
/// - `/pin-setup/otp?userRef=<id>` (+ `extra` = [PinSetupDraft]): OTP por
///   email + `POST /auth/pin/setup`.
/// - `/registration-success?userRef=<id>`: registro exitoso (fig `0:740`);
///   UNICO lugar donde se persiste el `user_ref` (SCR-005).
///
/// El PIN viaja SOLO en `extra` (memoria, [PinSetupDraft]): nunca en la ruta
/// ni en logs. Por compatibilidad se acepta el `String` suelto de F-T39
/// (equivale a un borrador solo con PIN, consentimiento `false`).
/// El feature NUNCA toca `lib/core/router/app_router.dart`: el orquestador
/// hace `...pinSetupRoutes` en su lista `routes`.
final List<GoRoute> pinSetupRoutes = [
  GoRoute(
    path: '/pin-setup',
    builder: (context, state) {
      final userRef = state.uri.queryParameters['userRef'] ?? '';
      if (userRef.isEmpty) {
        return Scaffold(
          appBar: AppBar(title: const Text('Crea tu PIN')),
          body: EmptyView(
            message: 'Falta la referencia de usuario. Vuelve al registro '
                'para generar un nuevo código.',
            actionLabel: 'Volver al registro',
            onAction: () => context.go('/kyc'),
          ),
        );
      }
      return PinCreatePage(userRef: userRef);
    },
    routes: [
      GoRoute(
        path: 'confirm',
        builder: (context, state) {
          final userRef = state.uri.queryParameters['userRef'] ?? '';
          final draft = PinSetupDraft.fromExtra(state.extra);
          if (userRef.isEmpty || !draft.isComplete) {
            return Scaffold(
              appBar: AppBar(title: const Text('Confirma tu PIN')),
              body: EmptyView(
                message: 'Primero crea tu PIN para poder confirmarlo.',
                actionLabel: 'Crear mi PIN',
                onAction: () => context.go(
                  '/pin-setup?userRef=${Uri.encodeComponent(userRef)}',
                ),
              ),
            );
          }
          return PinConfirmPage(userRef: userRef, pin: draft.pin);
        },
      ),
      GoRoute(
        path: 'biometrics',
        builder: (context, state) {
          final userRef = state.uri.queryParameters['userRef'] ?? '';
          final draft = PinSetupDraft.fromExtra(state.extra);
          if (userRef.isEmpty || !draft.isComplete) {
            return Scaffold(
              appBar: AppBar(title: const Text('Acceso biométrico')),
              body: EmptyView(
                message: 'Primero crea tu PIN para continuar.',
                actionLabel: 'Crear mi PIN',
                onAction: () => context.go(
                  '/pin-setup?userRef=${Uri.encodeComponent(userRef)}',
                ),
              ),
            );
          }
          return BiometricOfferPage(
            userRef: userRef,
            pin: draft.pin,
            reader: pinSetupBiometricReaderFactory?.call(),
          );
        },
      ),
      GoRoute(
        path: 'otp',
        builder: (context, state) {
          final setupFactory = pinSetupServiceFactory;
          final resendFactory = pinSetupResendServiceFactory;
          final userRef = state.uri.queryParameters['userRef'] ?? '';
          final draft = PinSetupDraft.fromExtra(state.extra);
          if (setupFactory == null ||
              resendFactory == null ||
              userRef.isEmpty ||
              !draft.isComplete) {
            return Scaffold(
              appBar: AppBar(title: const Text('Revisa tu correo')),
              body: ErrorView(
                message: userRef.isEmpty || !draft.isComplete
                    ? 'Falta la referencia de usuario. Vuelve al registro '
                        'para generar un nuevo código.'
                    : 'Verificación no disponible en este momento. '
                        'Inténtalo más tarde.',
              ),
            );
          }
          return PinSetupOtpPage(
            userRef: userRef,
            pin: draft.pin,
            biometricEnabled: draft.biometricEnabled,
            setupService: setupFactory(),
            resendService: resendFactory(),
            identity: sessionIdentityStoreFactory?.call(),
          );
        },
      ),
    ],
  ),
  GoRoute(
    path: '/registration-success',
    builder: (context, state) {
      final userRef = state.uri.queryParameters['userRef'] ?? '';
      return RegistrationSuccessPage(
        userRef: userRef,
        identity: sessionIdentityStoreFactory?.call(),
      );
    },
  ),
];
