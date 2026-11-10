import 'package:banca_online/core/notifications/notification_service.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('show registra y avisa a los listeners (addListener)', () {
    final service = NotificationService();
    var calls = 0;
    service.addListener(() => calls++);

    final n = service.show(
      title: 't',
      body: 'b',
      route: '/activate?userRef=u-1',
    );

    expect(n.id, isNotEmpty);
    expect(service.last, same(n));
    expect(service.notifications, hasLength(1));
    expect(calls, 1);
  });

  test('showOtpSent abre /activate con el userRef y no trae el código', () {
    final service = NotificationService();

    final n = service.showOtpSent(userRef: 'u-1');

    expect(n.route, '/activate?userRef=u-1');
    expect(n.title, isNotEmpty);
    // Honesto: el aviso NUNCA incluye un OTP de 6 dígitos.
    expect(RegExp(r'\b\d{6}\b').hasMatch(n.body), isFalse);
    expect(RegExp(r'\b\d{6}\b').hasMatch(n.title), isFalse);
  });

  test('showOtpSent codifica el userRef en la ruta', () {
    final service = NotificationService();

    final n = service.showOtpSent(userRef: 'a b/c?');

    expect(n.route, startsWith('/activate?userRef='));
    expect(n.route, contains(Uri.encodeComponent('a b/c?')));
  });

  test('tap propaga a los tap-listeners y tap desconocido retorna false',
      () {
    final service = NotificationService();
    AppNotification? tapped;
    service.addTapListener((n) => tapped = n);

    final n = service.showOtpSent(userRef: 'u-9');
    expect(service.tap(n.id), isTrue);
    expect(tapped?.route, '/activate?userRef=u-9');

    expect(service.tap('no-existe'), isFalse);
  });

  test('removeTapListener deja de propagar', () {
    final service = NotificationService();
    var calls = 0;
    void listener(AppNotification _) => calls++;
    service.addTapListener(listener);

    final n = service.showOtpSent(userRef: 'u-1');
    expect(service.tap(n.id), isTrue);
    expect(calls, 1);

    service.removeTapListener(listener);
    expect(service.tap(n.id), isTrue);
    expect(calls, 1);
  });
}
