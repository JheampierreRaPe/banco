import 'dart:async';

import 'package:banca_online/core/errors/api_exception.dart';
import 'package:banca_online/features/notifications/data/notifications_service.dart';
import 'package:banca_online/features/notifications/models/notification.dart';
import 'package:banca_online/features/notifications/presentation/notification_detail_page.dart';
import 'package:banca_online/features/notifications/presentation/notification_preferences_page.dart';
import 'package:banca_online/features/notifications/presentation/notifications_page.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Fake del servicio (seam `NotificationsServiceBase`): sin red, con
/// shapes reales `docs/05` §6.9 + fix A2 (`GET /notifications` -> `data` +
/// `meta` con `device`/`location`/`reference` opcionales por item;
/// `GET /notifications/{id}` -> detalle read-only con la misma vista
/// acotada; metadatos sin PII; reporte idempotente con `replayed`).
class FakeNotificationsService implements NotificationsServiceBase {
  FakeNotificationsService({
    List<NotificationItem>? seed,
    this.hangList = false,
    this.hangDetail = false,
    ChannelPreferences? prefs,
  })  : items = List.of(seed ?? _defaultSeed),
        preferences = prefs ??
            const ChannelPreferences(push: true, email: true, sms: false);

  static const _defaultSeed = [
    NotificationItem(
      id: 'n-1',
      channel: 'push',
      templateCode: 'security_operation_alert',
      status: 'SENT',
      createdAt: '2026-10-13T10:00:00Z',
      device: 'Pixel 8',
      location: 'Lima, PE (aprox.)',
      reference: 'OP-ABC123',
    ),
    NotificationItem(
      id: 'n-2',
      channel: 'email',
      templateCode: 'security_operation_email',
      status: 'SENT',
      createdAt: '2026-10-12T10:00:00Z',
      readAt: '2026-10-12T11:00:00Z',
      device: 'Chrome',
      location: 'Arequipa, PE (aprox.)',
      reference: 'OP-XYZ789',
    ),
  ];

  List<NotificationItem> items;
  ChannelPreferences preferences;
  final bool hangList;
  final bool hangDetail;

  int listCalls = 0;
  int detailCalls = 0;
  bool unreadFilterSeen = false;
  final List<String> readIds = [];
  final List<String> detailIds = [];
  final List<String> reportIds = [];
  final List<ChannelPreferences> savedPrefs = [];

  ApiException? listError;
  ApiException? detailError;
  ApiException? readError;
  ApiException? prefsError;
  ApiException? saveError;
  ApiException? reportError;
  bool reportReplayed = false;

  @override
  Future<NotificationPage> list({
    String? channel,
    bool unreadOnly = false,
    int page = 1,
    int pageSize = 20,
  }) async {
    listCalls++;
    unreadFilterSeen = unreadOnly;
    if (listError != null) throw listError!;
    if (hangList) await Completer<void>().future;
    var out = items;
    if (unreadOnly) out = out.where((e) => !e.isRead).toList();
    if (channel != null && channel.isNotEmpty) {
      out = out.where((e) => e.channel == channel).toList();
    }
    return NotificationPage(
      items: List.of(out),
      page: page,
      pageSize: pageSize,
      total: out.length,
    );
  }

  @override
  Future<NotificationItem> detail(String id) async {
    detailCalls++;
    detailIds.add(id);
    if (detailError != null) throw detailError!;
    if (hangDetail) await Completer<void>().future;
    final index = items.indexWhere((e) => e.id == id);
    if (index < 0) {
      throw ApiException(
        code: 'NOT_FOUND',
        message: 'No encontrado.',
        statusCode: 404,
      );
    }
    return items[index];
  }

  @override
  Future<NotificationItem> markRead(String id) async {
    readIds.add(id);
    if (readError != null) throw readError!;
    final index = items.indexWhere((e) => e.id == id);
    if (index < 0) {
      throw ApiException(code: 'NOT_FOUND', message: 'No encontrado.', statusCode: 404);
    }
    final current = items[index];
    final updated = NotificationItem(
      id: current.id,
      channel: current.channel,
      templateCode: current.templateCode,
      status: current.status,
      createdAt: current.createdAt,
      readAt: current.readAt ?? '2026-10-13T12:00:00Z',
      device: current.device,
      location: current.location,
      reference: current.reference,
    );
    items[index] = updated;
    return updated;
  }

  @override
  Future<ChannelPreferences> getPreferences() async {
    if (prefsError != null) throw prefsError!;
    return preferences;
  }

  @override
  Future<ChannelPreferences> updatePreferences(
    ChannelPreferences prefs,
  ) async {
    savedPrefs.add(prefs);
    if (saveError != null) throw saveError!;
    preferences = prefs;
    return prefs;
  }

  @override
  Future<ReportResult> report(String id, {String? reason}) async {
    reportIds.add(id);
    if (reportError != null) throw reportError!;
    return ReportResult(
      reported: true,
      replayed: reportReplayed,
      notificationId: id,
      confirmationId: 'CONF-1',
      reference: 'OP-ABC123',
    );
  }
}

Widget _wrap(Widget child) => MaterialApp(home: child);

void main() {
  group('Centro de notificaciones (E6-T15)', () {
    testWidgets('lista items con estado leído/no leído', (tester) async {
      final service = FakeNotificationsService();
      await tester.pumpWidget(_wrap(NotificationsPage(service: service)));
      await tester.pumpAndSettle();

      expect(find.byKey(const Key('notification-n-1')), findsOneWidget);
      expect(find.byKey(const Key('notification-n-2')), findsOneWidget);
      expect(find.text('No leída'), findsOneWidget);
      expect(find.text('Leída'), findsOneWidget);
    });

    testWidgets('marcar leída actualiza el estado', (tester) async {
      final service = FakeNotificationsService();
      await tester.pumpWidget(_wrap(NotificationsPage(service: service)));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('notification-read-n-1')));
      await tester.pumpAndSettle();

      expect(service.readIds, contains('n-1'));
      // Tras recargar, ambas quedan leídas.
      expect(find.text('No leída'), findsNothing);
    });

    testWidgets('filtro de no leídas solo pide unread al servidor',
        (tester) async {
      final service = FakeNotificationsService();
      await tester.pumpWidget(_wrap(NotificationsPage(service: service)));
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('notifications-unread-filter')));
      await tester.pumpAndSettle();

      expect(service.unreadFilterSeen, isTrue);
      expect(find.byKey(const Key('notification-n-1')), findsOneWidget);
      expect(find.byKey(const Key('notification-n-2')), findsNothing);
    });

    testWidgets('estado vacío sin notificaciones', (tester) async {
      final service = FakeNotificationsService(seed: []);
      await tester.pumpWidget(_wrap(NotificationsPage(service: service)));
      await tester.pumpAndSettle();

      expect(find.textContaining('No tienes notificaciones'), findsOneWidget);
    });

    testWidgets('estado de carga muestra el skeleton', (tester) async {
      final service = FakeNotificationsService(hangList: true);
      await tester.pumpWidget(_wrap(NotificationsPage(service: service)));
      await tester.pump();

      expect(find.byKey(const Key('notifications-skeleton')), findsOneWidget);
    });

    testWidgets('estado de error con Reintentar', (tester) async {
      final service = FakeNotificationsService()
        ..listError = ApiException.network();
      await tester.pumpWidget(_wrap(NotificationsPage(service: service)));
      await tester.pumpAndSettle();

      expect(find.textContaining('conexión a internet'), findsOneWidget);
      service.listError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('notification-n-1')), findsOneWidget);
    });
  });

  group('Detalle + "no reconozco" (E6-T15)', () {
    const item = NotificationItem(
      id: 'n-1',
      channel: 'push',
      templateCode: 'security_operation_alert',
      status: 'SENT',
      createdAt: '2026-10-13T10:00:00Z',
      device: 'Pixel 8',
      location: 'Lima, PE (aprox.)',
      reference: 'OP-ABC123',
    );

    testWidgets('muestra dispositivo, hora, ubicación y referencia',
        (tester) async {
      final service = FakeNotificationsService();
      await tester.pumpWidget(
        _wrap(
          NotificationDetailPage(
            notificationId: 'n-1',
            notification: item,
            service: service,
          ),
        ),
      );
      await tester.pumpAndSettle();

      // Fuente de verdad: `GET /notifications/{id}` (fix A2), no el extra.
      expect(service.detailIds, contains('n-1'));
      expect(find.text('Pixel 8'), findsOneWidget);
      expect(find.text('2026-10-13T10:00:00Z'), findsOneWidget);
      expect(find.text('Lima, PE (aprox.)'), findsOneWidget);
      expect(find.text('OP-ABC123'), findsOneWidget);
    });

    testWidgets('el detalle se carga del servidor aunque no haya extra',
        (tester) async {
      final service = FakeNotificationsService();
      await tester.pumpWidget(
        _wrap(
          NotificationDetailPage(
            notificationId: 'n-2',
            service: service,
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(service.detailIds, contains('n-2'));
      expect(find.text('Chrome'), findsOneWidget);
      expect(find.text('OP-XYZ789'), findsOneWidget);
    });

    testWidgets('sin servicio muestra error accionable', (tester) async {
      await tester.pumpWidget(
        _wrap(
          const NotificationDetailPage(
            notificationId: 'n-2',
            service: null,
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('no configurado'), findsOneWidget);
    });

    testWidgets('estado de carga del detalle', (tester) async {
      final service = FakeNotificationsService(hangDetail: true);
      await tester.pumpWidget(
        _wrap(
          NotificationDetailPage(
            notificationId: 'n-1',
            notification: item,
            service: service,
          ),
        ),
      );
      await tester.pump();

      expect(
        find.byKey(const Key('notification-detail-loading')),
        findsOneWidget,
      );
    });

    testWidgets('404 al cargar el detalle muestra mensaje neutro',
        (tester) async {
      final service = FakeNotificationsService()
        ..detailError = ApiException(
          code: 'NOT_FOUND',
          message: 'No encontrado.',
          statusCode: 404,
        );
      await tester.pumpWidget(
        _wrap(
          NotificationDetailPage(
            notificationId: 'n-1',
            service: service,
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('ya no está disponible'), findsOneWidget);
      expect(find.text('Reintentar'), findsOneWidget);

      // Reintentar vuelve a pedir el detalle al servidor y muestra el
      // contenido del servidor.
      service.detailError = null;
      await tester.tap(find.text('Reintentar'));
      await tester.pumpAndSettle();
      expect(service.detailCalls, greaterThanOrEqualTo(2));
      expect(find.text('Pixel 8'), findsOneWidget);
    });

    testWidgets('campos ausentes muestran texto neutro', (tester) async {
      final service = FakeNotificationsService(
        seed: const [
          NotificationItem(
            id: 'n-old',
            channel: 'push',
            templateCode: 'security_operation_alert',
            status: 'SENT',
            createdAt: '2026-10-10T10:00:00Z',
          ),
        ],
      );
      await tester.pumpWidget(
        _wrap(
          NotificationDetailPage(
            notificationId: 'n-old',
            service: service,
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(service.detailIds, contains('n-old'));
      expect(find.text('No informado por el servidor'), findsOneWidget);
      expect(find.text('No informada por el servidor'), findsWidgets);
    });

    testWidgets(
        '"no reconozco" pide confirmación, llama al endpoint y muestra '
        'el resultado', (tester) async {
      final service = FakeNotificationsService();
      await tester.pumpWidget(
        _wrap(
          NotificationDetailPage(
            notificationId: 'n-1',
            notification: item,
            service: service,
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('notification-report-action')));
      await tester.pumpAndSettle();

      // Confirmación neutral con advertencia de bloqueo.
      expect(find.textContaining('bloquearán tus canales'), findsWidgets);
      await tester.tap(find.byKey(const Key('report-confirm')));
      await tester.pumpAndSettle();

      expect(service.reportIds, contains('n-1'));
      expect(
        find.byKey(const Key('notification-report-result')),
        findsOneWidget,
      );
      expect(find.textContaining('bloquearon tus canales'), findsWidgets);
    });

    testWidgets('cancelar la confirmación no llama al endpoint',
        (tester) async {
      final service = FakeNotificationsService();
      await tester.pumpWidget(
        _wrap(
          NotificationDetailPage(
            notificationId: 'n-1',
            notification: item,
            service: service,
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('notification-report-action')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('report-cancel')));
      await tester.pumpAndSettle();

      expect(service.reportIds, isEmpty);
    });

    testWidgets('404 al reportar muestra mensaje neutro', (tester) async {
      final service = FakeNotificationsService()
        ..reportError = ApiException(
          code: 'NOT_FOUND',
          message: 'No encontrado.',
          statusCode: 404,
        );
      await tester.pumpWidget(
        _wrap(
          NotificationDetailPage(
            notificationId: 'n-1',
            notification: item,
            service: service,
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('notification-report-action')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('report-confirm')));
      await tester.pumpAndSettle();

      expect(find.textContaining('ya no está disponible'), findsOneWidget);
    });

    testWidgets('reporte idempotente repetido muestra estado protegido',
        (tester) async {
      final service = FakeNotificationsService()..reportReplayed = true;
      await tester.pumpWidget(
        _wrap(
          NotificationDetailPage(
            notificationId: 'n-1',
            notification: item,
            service: service,
          ),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byKey(const Key('notification-report-action')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('report-confirm')));
      await tester.pumpAndSettle();

      expect(service.reportIds, contains('n-1'));
      expect(
        find.byKey(const Key('notification-report-result')),
        findsOneWidget,
      );
      expect(find.textContaining('ya estaba registrado'), findsWidgets);
    });
  });

  group('Contrato real docs/05 §6.9 + fix A2 (E6-T15)', () {
    test('la lista parsea device/location/reference por item', () {
      final page = NotificationPage.fromJson({
        'data': [
          {
            'id': 'n-1',
            'channel': 'push',
            'template_code': 'security_operation_alert',
            'status': 'SENT',
            'created_at': '2026-10-13T10:00:00Z',
            'read_at': null,
            'device': 'Pixel 8',
            'location': 'Lima, PE (aprox.)',
            'reference': 'OP-ABC123',
          },
          {
            // Fila vieja sin render acotado: `null`, sin PII extra.
            'id': 'n-old',
            'channel': 'email',
            'template_code': 'security_operation_email',
            'status': 'SENT',
            'created_at': '2026-10-10T10:00:00Z',
            'read_at': null,
            'device': null,
            'location': null,
            'reference': null,
          },
        ],
        'meta': {'page': 1, 'page_size': 20, 'total': 2},
      });

      expect(page.items, hasLength(2));
      expect(page.items.first.device, 'Pixel 8');
      expect(page.items.first.location, 'Lima, PE (aprox.)');
      expect(page.items.first.reference, 'OP-ABC123');
      expect(page.items.last.device, isNull);
      expect(page.items.last.location, isNull);
      expect(page.items.last.reference, isNull);
    });

    test('el detalle parsea la misma vista acotada', () {
      final item = NotificationItem.fromJson({
        'id': 'n-1',
        'channel': 'push',
        'template_code': 'security_operation_alert',
        'status': 'SENT',
        'created_at': '2026-10-13T10:00:00Z',
        'read_at': null,
        'device': 'Pixel 8',
        'location': 'Lima, PE (aprox.)',
        'reference': 'OP-ABC123',
      });

      expect(item.device, 'Pixel 8');
      expect(item.location, 'Lima, PE (aprox.)');
      expect(item.reference, 'OP-ABC123');
    });
  });

  group('Preferencias por canal (E6-T15)', () {
    testWidgets('se leen del backend y se guardan', (tester) async {
      final service = FakeNotificationsService();
      await tester.pumpWidget(
        _wrap(NotificationPreferencesPage(service: service)),
      );
      await tester.pumpAndSettle();

      // Refleja lo del servidor (push on, sms off).
      final sms = tester.widget<SwitchListTile>(
        find.byKey(const Key('prefs-sms')),
      );
      expect(sms.value, isFalse);

      await tester.tap(find.byKey(const Key('prefs-sms')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('prefs-save')));
      await tester.pumpAndSettle();

      expect(service.savedPrefs, hasLength(1));
      expect(service.savedPrefs.single.sms, isTrue);
      expect(find.text('Preferencias guardadas.'), findsOneWidget);
    });

    testWidgets('muestra la nota del respaldo de seguridad por correo',
        (tester) async {
      final service = FakeNotificationsService();
      await tester.pumpWidget(
        _wrap(NotificationPreferencesPage(service: service)),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('respaldo de seguridad'), findsOneWidget);
    });

    testWidgets('error de red muestra estado error', (tester) async {
      final service = FakeNotificationsService()
        ..prefsError = ApiException.network();
      await tester.pumpWidget(
        _wrap(NotificationPreferencesPage(service: service)),
      );
      await tester.pumpAndSettle();

      expect(find.text('Reintentar'), findsOneWidget);
    });
  });

  group('Cliente delgado (E6-T15)', () {
    test('el servicio no decide bloqueos: solo presenta lo del servidor',
        () async {
      final service = FakeNotificationsService();
      final page = await service.list();
      expect(page.items, hasLength(2));

      final result = await service.report('n-1');
      expect(result.reported, isTrue);
      expect(result.replayed, isFalse);
      // El bloqueo vive en el servidor; el cliente solo recibe el
      // resultado (`reported`/`replayed`), sin lógica local.
    });
  });
}
