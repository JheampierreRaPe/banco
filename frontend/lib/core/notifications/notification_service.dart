import 'package:flutter/foundation.dart';

/// Notificación local en memoria (núcleo, sin plugins nativos).
///
/// - Es infraestructura REAL dentro del dispositivo: [show] registra la
///   notificación y avisa a los listeners (`ChangeNotifier.addListener`); el
///   tap se propaga vía [addTapListener]/[tap] para que la UI navegue.
/// - NO muestra ni transporta el código OTP: el backend mock (E1-T09/E1-T10,
///   verificado en `backend/tests/test_activation.py`) nunca devuelve el
///   código en `POST /auth/activate` ni en `POST /auth/otp/resend`
///   (`assert plain not in resp.text`), así que inventarlo en cliente
///   rompería seguridad. El push con el código exige proveedor real (HU22,
///   sprint 4). Ver `activation_notifications.dart` para el copy honesto.
/// - Sin PII en logs: esta clase no loguea nada.
@immutable
class AppNotification {
  const AppNotification({
    required this.id,
    required this.title,
    required this.body,
    required this.route,
  });

  /// Identificador secuencial (`n-1`, `n-2`, ...) solo para tests/tap.
  final String id;

  /// Título corto visible (p. ej. `Código enviado`).
  final String title;

  /// Cuerpo visible. JAMÁS contiene el OTP.
  final String body;

  /// Ruta deep-link a abrir al tocar (p. ej. `/activate?userRef=...`).
  final String route;
}

/// Callback de tap sobre una notificación (equivale al tap del SO).
typedef NotificationTapCallback = void Function(AppNotification notification);

/// Servicio de notificaciones locales del dispositivo.
///
/// Uso:
/// ```dart
/// final notifications = NotificationService();
/// notifications.addTapListener((n) => context.go(n.route));
/// notifications.showOtpSent(userRef: userRef); // tras resend exitoso
/// ```
class NotificationService extends ChangeNotifier {
  int _seq = 0;
  final List<AppNotification> _items = [];
  final List<NotificationTapCallback> _tapListeners = [];

  /// Historial (solo lectura). La última es la más reciente.
  List<AppNotification> get notifications => List.unmodifiable(_items);

  /// Última notificación mostrada, o `null` si no hay ninguna.
  AppNotification? get last => _items.isEmpty ? null : _items.last;

  /// Registra una notificación genérica y notifica a los listeners.
  AppNotification show({
    required String title,
    required String body,
    required String route,
  }) {
    _seq++;
    final notification = AppNotification(
      id: 'n-$_seq',
      title: title,
      body: body,
      route: route,
    );
    _items.add(notification);
    notifyListeners();
    return notification;
  }

  /// Atajo honesto para el OTP: aviso informativo SIN el código.
  ///
  /// El cuerpo nunca incluye dígitos del OTP (ver garantía en el doc de
  /// [AppNotification]). El tap abre `/activate?userRef=<userRef>`.
  AppNotification showOtpSent({required String userRef}) {
    return show(
      title: 'Código enviado',
      body: 'Te enviamos un nuevo código por SMS. '
          'Revísalo en tus mensajes e ingrésalo aquí. '
          'Toca para abrir la activación.',
      route: '/activate?userRef=${Uri.encodeComponent(userRef)}',
    );
  }

  /// Registra un listener de tap (apertura por toque).
  void addTapListener(NotificationTapCallback listener) {
    _tapListeners.add(listener);
  }

  /// Retira un listener de tap previamente registrado.
  void removeTapListener(NotificationTapCallback listener) {
    _tapListeners.remove(listener);
  }

  /// Simula el tap del SO sobre la notificación [id].
  ///
  /// Retorna `true` si existía y se notificó a los listeners.
  bool tap(String id) {
    AppNotification? found;
    for (final item in _items) {
      if (item.id == id) {
        found = item;
        break;
      }
    }
    if (found == null) return false;
    final notification = found;
    for (final listener in List<NotificationTapCallback>.from(_tapListeners)) {
      listener(notification);
    }
    return true;
  }

  /// Limpia el historial (útil en tests).
  void clear() {
    _items.clear();
    notifyListeners();
  }
}
