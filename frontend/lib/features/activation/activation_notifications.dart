import 'package:banca_online/core/notifications/notification_service.dart';

/// Copy honesto del aviso OTP (feature `activation`, aditivo).
///
/// El backend mock (E1-T09/E1-T10) envía el OTP por canal SMS simulado y
/// NUNCA lo devuelve en las respuestas (`backend/tests/test_activation.py`:
/// `assert plain not in resp.text` en activate y resend). Por eso la app NO
/// puede mostrar el código real: muestra un aviso local informativo y un
/// deep-link a `/activate`. Mostrar el código exige proveedor push real
/// (HU22, sprint 4).
class ActivationNotifications {
  ActivationNotifications._();

  /// Título del aviso local tras un resend exitoso.
  static const String otpSentTitle = 'Código enviado';

  /// Cuerpo del aviso local. Sin OTP, sin PII.
  static const String otpSentBody =
      'Te enviamos un nuevo código por SMS. '
      'Revísalo en tus mensajes e ingrésalo aquí. '
      'Toca para abrir la activación.';

  /// Título del diálogo explicativo.
  static const String howItArrivesTitle = 'Cómo llega el código';

  /// Explicación honesta del canal mock (sin inventar el código).
  static const String howItArrivesBody =
      'Te enviamos el código de 6 dígitos por SMS a tu número registrado.\n\n'
      'Esta versión usa un canal de demostración: la app te avisa con una '
      'notificación local cuando el código se reenvía, pero el código viaja '
      'por SMS y debes copiarlo desde tus mensajes.\n\n'
      'Mostrar el código dentro del aviso exige un proveedor de notificaciones '
      'real (previsto en HU22).';

  /// Deep-link que abre la pantalla de activación con el [userRef].
  static String activateRoute(String userRef) =>
      '/activate?userRef=${Uri.encodeComponent(userRef)}';

  /// Construye el aviso OTP sin mostrarlo (testeable sin servicio).
  static AppNotification buildOtpSent(String userRef) => AppNotification(
        id: 'activation-otp',
        title: otpSentTitle,
        body: otpSentBody,
        route: activateRoute(userRef),
      );
}
