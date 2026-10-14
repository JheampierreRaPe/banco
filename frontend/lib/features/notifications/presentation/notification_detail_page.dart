import 'package:flutter/material.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/error_view.dart';
import '../data/notifications_service.dart';
import '../models/notification.dart';

/// Detalle de la alerta + accion "no reconozco esta operacion"
/// (HU22, E6-T15; contrato `docs/05` §6.9, fix A2).
///
/// - El contenido viene del servidor (`GET /notifications/{id}`, detalle
///   read-only, solo titular): operacion/referencia, canal, dispositivo,
///   hora (`created_at`) y ubicacion aproximada. El `extra` de la lista es
///   solo un indicio de navegacion; la fuente de verdad es el detalle.
/// - Muestra dispositivo/hora/ubicacion/referencia con los valores del
///   servidor y texto neutro si vienen ausentes (`null` en filas viejas).
/// - La accion pide confirmacion neutral (advierte que se bloquearan los
///   canales digitales) y presenta el resultado del servidor (cliente
///   delgado: la app no decide el bloqueo).
/// - `404` neutro -> "no disponible"; red/error -> mensaje accionable.
///
/// Estados (docs/20 §7): contenido, carga, error + Reintentar. Si el
/// servicio no esta configurado, error accionable. Mensajes en espanol.
/// Sin PII en logs (nunca se imprime el item).
class NotificationDetailPage extends StatefulWidget {
  const NotificationDetailPage({
    super.key,
    required this.notificationId,
    this.notification,
    this.service,
  });

  /// Id de la alerta (ruta `/notifications/:id`).
  final String notificationId;

  /// Item ya listado (viaja por `extra`); solo indicio de navegacion, la
  /// fuente de verdad es `GET /notifications/{id}`.
  final NotificationItem? notification;

  final NotificationsServiceBase? service;

  @override
  State<NotificationDetailPage> createState() => _NotificationDetailPageState();
}

class _NotificationDetailPageState extends State<NotificationDetailPage> {
  Future<NotificationItem>? _future;
  bool _reporting = false;

  @override
  void initState() {
    super.initState();
    _future = widget.service?.detail(widget.notificationId);
  }

  @override
  void didUpdateWidget(covariant NotificationDetailPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.service != widget.service ||
        oldWidget.notificationId != widget.notificationId) {
      _future = widget.service?.detail(widget.notificationId);
    }
  }

  void _retry() {
    final service = widget.service;
    if (service == null) {
      setState(() {});
      return;
    }
    setState(() {
      _future = service.detail(widget.notificationId);
    });
  }

  Future<void> _confirmReport() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => const _ReportConfirmDialog(),
    );
    if (confirmed != true || !mounted) return;
    await _performReport();
  }

  /// Envia el reporte y presenta el resultado del servidor.
  Future<void> _performReport() async {
    final service = widget.service;
    if (service == null || _reporting) return;
    setState(() => _reporting = true);
    try {
      final result = await service.report(widget.notificationId);
      if (!mounted) return;
      final replayed = result.replayed;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            replayed
                ? 'Este reporte ya estaba registrado. Tus canales '
                    'digitales siguen protegidos.'
                : 'Reporte registrado. Por seguridad se bloquearon tus '
                    'canales digitales. Te avisaremos por correo.',
          ),
        ),
      );
      if (mounted) {
        showDialog<void>(
          context: context,
          builder: (context) => _ReportResultDialog(result: result),
        ).then((_) {
          if (mounted) Navigator.of(context).maybePop(true);
        });
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(_reportErrorMessage(e))),
      );
    } finally {
      if (mounted) setState(() => _reporting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final service = widget.service;
    if (service == null) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        appBar: AppBar(title: const Text('Detalle de alerta')),
        body: const Padding(
          padding: EdgeInsets.all(16),
          child: ErrorView(
            message: 'Servicio de notificaciones no configurado. '
                'Intentalo mas tarde.',
          ),
        ),
      );
    }
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Detalle de alerta')),
      body: FutureBuilder<NotificationItem>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const Center(
              key: Key('notification-detail-loading'),
              child: Padding(
                padding: EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    CircularProgressIndicator(),
                    SizedBox(height: 12),
                    Text('Cargando alerta…'),
                  ],
                ),
              ),
            );
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            final message = error is ApiException
                ? _detailErrorMessage(error)
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
          final item = snapshot.data;
          if (item == null) {
            return Padding(
              padding: const EdgeInsets.all(16),
              child: ErrorView(
                message: 'Esta alerta ya no está disponible. Actualiza tu '
                    'centro de notificaciones.',
                retryLabel: 'Reintentar',
                onRetry: _retry,
              ),
            );
          }
          return _detailContent(item);
        },
      ),
    );
  }

  Widget _detailContent(NotificationItem item) {
    return ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          AppCard(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    const Icon(
                      Icons.shield_outlined,
                      color: AppColors.primary,
                      size: 24,
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        'Alerta de seguridad',
                        style: AppTypography.titleMd.copyWith(fontSize: 16),
                      ),
                    ),
                    if (item.isRead)
                      const AppStatusChip.success(label: 'Leída')
                    else
                      const AppStatusChip.warning(label: 'No leída'),
                  ],
                ),
                const SizedBox(height: 12),
                _DetailRow(
                  label: 'Operación',
                  value: item.templateCode.isNotEmpty
                      ? item.templateCode.replaceAll('_', ' ')
                      : 'Alerta de seguridad',
                ),
                _DetailRow(
                  label: 'Referencia',
                  value: (item.reference?.isNotEmpty ?? false)
                      ? item.reference!
                      : 'No informada por el servidor',
                ),
                _DetailRow(
                  label: 'Canal',
                  value: _channelLabel(item.channel),
                ),
                _DetailRow(
                  label: 'Dispositivo',
                  value: (item.device?.isNotEmpty ?? false)
                      ? item.device!
                      : 'No informado por el servidor',
                ),
                _DetailRow(
                  label: 'Hora',
                  value: item.createdAt.isNotEmpty
                      ? item.createdAt
                      : 'No informada por el servidor',
                ),
                _DetailRow(
                  label: 'Ubicación aproximada',
                  value: (item.location?.isNotEmpty ?? false)
                      ? item.location!
                      : 'No informada por el servidor',
                ),
                _DetailRow(
                  label: 'Estado',
                  value: item.status.isEmpty ? 'Sin estado' : item.status,
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
          AppCard(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  '¿No reconoces esta operación?',
                  style: AppTypography.titleMd.copyWith(fontSize: 15),
                ),
                const SizedBox(height: 6),
                Text(
                  'Si reportas esta alerta, por seguridad se bloquearán '
                  'tus canales digitales y revisaremos la operación. '
                  'Solo hazlo si realmente no la reconoces.',
                  style: AppTypography.bodyMd.copyWith(
                    fontSize: 13,
                    color: AppColors.secondaryText,
                  ),
                ),
                const SizedBox(height: 12),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton.icon(
                    key: const Key('notification-report-action'),
                    style: FilledButton.styleFrom(
                      backgroundColor: AppColors.error,
                      minimumSize: const Size(0, 52),
                    ),
                    onPressed: _reporting ? null : _confirmReport,
                    icon: _reporting
                        ? const SizedBox.square(
                            dimension: 20,
                            child: CircularProgressIndicator(
                              strokeWidth: 2.5,
                              color: Colors.white,
                            ),
                          )
                        : const Icon(Icons.report_outlined, size: 20),
                    label: Text(
                      _reporting
                          ? 'Enviando reporte…'
                          : 'No reconozco esta operación',
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      );
  }
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
      return channel.isEmpty ? 'No informado' : channel;
  }
}

/// Confirmacion neutral antes de enviar el reporte.
class _ReportConfirmDialog extends StatelessWidget {
  const _ReportConfirmDialog();

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      title: const Text('¿Reportar esta operación?'),
      content: const Text(
        'Al confirmar se bloquearán tus canales digitales por seguridad '
        'y el equipo revisará la operación. Esta acción no se puede deshacer '
        'desde la app.',
      ),
      actions: [
        TextButton(
          key: const Key('report-cancel'),
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text('Cancelar'),
        ),
        FilledButton(
          key: const Key('report-confirm'),
          style: FilledButton.styleFrom(backgroundColor: AppColors.error),
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text('Sí, reportar'),
        ),
      ],
    );
  }
}

/// Resultado del reporte tal cual del servidor (sin detalles sensibles).
class _ReportResultDialog extends StatelessWidget {
  const _ReportResultDialog({required this.result});

  final ReportResult result;

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      key: const Key('notification-report-result'),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      title: const Text('Reporte registrado'),
      content: Text(
        result.replayed
            ? 'Este reporte ya estaba registrado. Tus canales digitales '
                'siguen protegidos y te avisaremos por correo.'
            : 'Recibimos tu reporte. Por seguridad se bloquearon tus '
                'canales digitales y te avisaremos por correo.',
      ),
      actions: [
        FilledButton(
          key: const Key('report-result-ok'),
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Entendido'),
        ),
      ],
    );
  }
}

class _DetailRow extends StatelessWidget {
  const _DetailRow({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 150,
            child: Text(
              label,
              style: AppTypography.bodyMd.copyWith(
                fontSize: 13,
                color: AppColors.secondaryText,
              ),
            ),
          ),
          Expanded(
            child: Text(
              value,
              style: AppTypography.bodyMd.copyWith(fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }
}

/// `404` neutro -> "no disponible"; resto accionable (sin PII).
String _detailErrorMessage(ApiException error) {
  if (error.code == 'NOT_FOUND' || error.statusCode == 404) {
    return 'Esta alerta ya no está disponible. Actualiza tu centro de '
        'notificaciones.';
  }
  return error.message;
}

/// `404` neutro -> "no disponible"; resto accionable (sin PII).
String _reportErrorMessage(ApiException error) {
  if (error.code == 'NOT_FOUND' || error.statusCode == 404) {
    return 'Esta alerta ya no está disponible. Actualiza tu centro de '
        'notificaciones.';
  }
  return error.message;
}
