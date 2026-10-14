import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../data/notifications_service.dart';
import '../models/notification.dart';

/// Centro de notificaciones del titular (HU22, E6-T15; sin mockup en
/// `pantallas.fig` — fila pendiente `notificaciones-centro.png`; UI desde
/// tokens `docs/20`).
///
/// - Lista lo que devuelve el backend (metadatos enmascarados; cliente
///   delgado: sin calculos locales).
/// - Marca leidas (`POST /notifications/{id}/read`, idempotente).
/// - Entrada al detalle (operacion/dispositivo/hora/ubicacion aproximada)
///   y a las preferencias por canal.
///
/// Estados (docs/20 §7): contenido, carga (skeleton), vacio, error +
/// Reintentar. Mensajes en espanol.
class NotificationsPage extends StatefulWidget {
  const NotificationsPage({super.key, this.service});

  final NotificationsServiceBase? service;

  @override
  State<NotificationsPage> createState() => _NotificationsPageState();
}

class _NotificationsPageState extends State<NotificationsPage> {
  Future<NotificationPage>? _future;
  bool _unreadOnly = false;

  /// Id en curso de "marcar leida" (inhabilita su boton).
  String? _markingId;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<NotificationPage> _load() {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de notificaciones no configurado.',
        ),
      );
    }
    return service.list(unreadOnly: _unreadOnly);
  }

  void _retry() {
    setState(() {
      _future = _load();
    });
  }

  Future<void> _refresh() async {
    final future = _load();
    setState(() {
      _future = future;
    });
    try {
      await future;
    } catch (_) {
      // El FutureBuilder muestra el error; el refresh solo reintenta.
    }
  }

  void _toggleUnread(bool value) {
    setState(() {
      _unreadOnly = value;
      _future = _load();
    });
  }

  void _openPreferences() {
    context.push('/notifications/preferences');
  }

  Future<void> _openDetail(NotificationItem item) async {
    final changed = await context.push<bool>(
      '/notifications/${item.id}',
      extra: item,
    );
    if (changed == true && mounted) _retry();
  }

  /// Marca leida y refleja lo que responde el servidor (cliente delgado:
  /// recarga la lista; repetir es idempotente).
  Future<void> _markRead(NotificationItem item) async {
    final service = widget.service;
    if (service == null || _markingId != null || item.isRead) return;
    setState(() => _markingId = item.id);
    try {
      await service.markRead(item.id);
      if (!mounted) return;
      _retry();
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(e.message)),
      );
    } finally {
      if (mounted) setState(() => _markingId = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        title: const Text('Notificaciones'),
        actions: [
          IconButton(
            key: const Key('notifications-preferences-action'),
            tooltip: 'Preferencias de canal',
            icon: const Icon(Icons.tune_outlined, size: 24),
            onPressed: _openPreferences,
          ),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
            child: Row(
              children: [
                const Icon(
                  Icons.shield_outlined,
                  size: 20,
                  color: AppColors.secondaryText,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Alertas de seguridad de tu cuenta, tal cual las '
                    'registra el servidor.',
                    style: AppTypography.bodyMd.copyWith(
                      fontSize: 12,
                      color: AppColors.secondaryText,
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                FilterChip(
                  key: const Key('notifications-unread-filter'),
                  label: const Text('No leídas'),
                  selected: _unreadOnly,
                  onSelected: _toggleUnread,
                ),
              ],
            ),
          ),
          Expanded(
            child: FutureBuilder<NotificationPage>(
              future: _future,
              builder: (context, snapshot) {
                if (snapshot.connectionState == ConnectionState.waiting) {
                  return const _NotificationsSkeleton();
                }
                if (snapshot.hasError) {
                  final error = snapshot.error;
                  final message = error is ApiException
                      ? _errorMessage(error)
                      : 'Ocurrió un error al consultar el servidor. '
                          'Revisa tu conexión a internet o intenta de nuevo.';
                  return Padding(
                    padding: const EdgeInsets.all(16),
                    child: ErrorView(
                      message: message,
                      retryLabel: 'Reintentar',
                      onRetry: _retry,
                    ),
                  );
                }
                final page = snapshot.data ?? NotificationPage.empty;
                if (page.items.isEmpty) {
                  return RefreshIndicator(
                    onRefresh: _refresh,
                    child: ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: [
                        const SizedBox(height: 48),
                        EmptyView(
                          message: _unreadOnly
                              ? 'No tienes notificaciones sin leer.\n'
                                  'Te avisaremos aquí ante cualquier alerta '
                                  'de seguridad.'
                              : 'No tienes notificaciones.\n'
                                  'Te avisaremos aquí ante cualquier alerta '
                                  'de seguridad.',
                        ),
                      ],
                    ),
                  );
                }
                return RefreshIndicator(
                  onRefresh: _refresh,
                  child: ListView.builder(
                    physics: const AlwaysScrollableScrollPhysics(),
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
                    itemCount: page.items.length,
                    itemBuilder: (context, index) {
                      final item = page.items[index];
                      return Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: _NotificationCard(
                          item: item,
                          marking: _markingId == item.id,
                          onOpen: () => _openDetail(item),
                          onMarkRead: () => _markRead(item),
                        ),
                      );
                    },
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

/// Tarjeta de alerta (docs/20 §6: card blanca, radio 16, icono en circulo).
class _NotificationCard extends StatelessWidget {
  const _NotificationCard({
    required this.item,
    required this.onOpen,
    required this.onMarkRead,
    this.marking = false,
  });

  final NotificationItem item;
  final VoidCallback onOpen;
  final VoidCallback onMarkRead;
  final bool marking;

  @override
  Widget build(BuildContext context) {
    return AppCard(
      key: Key('notification-${item.id}'),
      padding: const EdgeInsets.all(14),
      child: InkWell(
        key: Key('notification-open-${item.id}'),
        borderRadius: BorderRadius.circular(12),
        onTap: onOpen,
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              width: 44,
              height: 44,
              alignment: Alignment.center,
              decoration: const BoxDecoration(
                shape: BoxShape.circle,
                color: AppColors.surfaceContainerHigh,
              ),
              child: Icon(
                item.isRead
                    ? Icons.notifications_none_outlined
                    : Icons.notifications_active_outlined,
                color: AppColors.primary,
                size: 24,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          _titleFor(item),
                          style: AppTypography.titleMd.copyWith(fontSize: 15),
                        ),
                      ),
                      const SizedBox(width: 8),
                      if (item.isRead)
                        const AppStatusChip.success(label: 'Leída')
                      else
                        const AppStatusChip.warning(label: 'No leída'),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    _subtitleFor(item),
                    style: AppTypography.bodyMd.copyWith(
                      fontSize: 12,
                      color: AppColors.secondaryText,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Row(
                    children: [
                      AppStatusChip.success(
                        label: _channelLabel(item.channel),
                      ),
                      const Spacer(),
                      if (!item.isRead)
                        marking
                            ? const SizedBox.square(
                                dimension: 20,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2.5,
                                ),
                              )
                            : TextButton(
                                key: Key('notification-read-${item.id}'),
                                style: TextButton.styleFrom(
                                  minimumSize: const Size(0, 36),
                                  tapTargetSize:
                                      MaterialTapTargetSize.shrinkWrap,
                                ),
                                onPressed: onMarkRead,
                                child: const Text('Marcar leída'),
                              ),
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

String _titleFor(NotificationItem item) {
  if (item.templateCode.isNotEmpty) {
    return item.templateCode.replaceAll('_', ' ');
  }
  return 'Alerta de seguridad';
}

String _subtitleFor(NotificationItem item) {
  final parts = <String>[];
  if (item.createdAt.isNotEmpty) parts.add(item.createdAt);
  if ((item.device ?? '').isNotEmpty) parts.add(item.device!);
  if ((item.location ?? '').isNotEmpty) parts.add(item.location!);
  if (parts.isEmpty) return 'Revisa el detalle para más información.';
  return parts.join(' · ');
}

String _channelLabel(String channel) {
  switch (channel.toLowerCase()) {
    case 'push':
      return 'Push';
    case 'email':
      return 'Correo';
    case 'sms':
      return 'SMS';
    default:
      return channel.isEmpty ? 'Aviso' : channel;
  }
}

/// Skeleton de carga (barras `#E4E2DD`).
class _NotificationsSkeleton extends StatelessWidget {
  const _NotificationsSkeleton();

  @override
  Widget build(BuildContext context) {
    return ListView(
      key: const Key('notifications-skeleton'),
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
      children: [
        for (var i = 0; i < 3; i++) ...[
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLowest,
              borderRadius: BorderRadius.circular(14),
            ),
            child: Row(
              children: [
                Container(
                  width: 44,
                  height: 44,
                  decoration: const BoxDecoration(
                    color: AppColors.surfaceContainerHigh,
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Container(
                        height: 14,
                        width: 144,
                        decoration: BoxDecoration(
                          color: AppColors.surfaceContainerHighest,
                          borderRadius: BorderRadius.circular(7),
                        ),
                      ),
                      const SizedBox(height: 8),
                      Container(
                        height: 10,
                        width: 96,
                        decoration: BoxDecoration(
                          color: AppColors.surfaceContainerHigh,
                          borderRadius: BorderRadius.circular(5),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
        ],
      ],
    );
  }
}

/// Mensaje accionable por codigo (sin tocar `core`, fuera de frontera).
String _errorMessage(ApiException error) {
  if (error.isNetworkError ||
      error.code == 'UNKNOWN' ||
      error.code == 'SERVER_ERROR') {
    return 'Ocurrió un error al consultar el servidor. '
        'Revisa tu conexión a internet o intenta de nuevo.';
  }
  return error.message;
}
