import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/movements_service.dart';
import '../models/movement.dart';

/// Detalle de movimiento + reverso (mockups `Reverso 1/2/3/4`).
///
/// - Hero con monto, estado, fecha y Op copiable (datos enmascarados).
/// - Desglose de la operacion (valores del servidor; el cliente no calcula).
/// - Constancia oficial (E2-T10) con descarga informativa.
/// - Reverso (E2-T25): solo si el estado es reversible
///   (`POSTED`/`SETTLED`/`CONCILIATED`) y dentro de 72h; confirmacion con
///   motivo opcional (<= 280) + `Idempotency-Key`. Si no reversible, el boton
///   va deshabilitado con su explicacion. El `409` se informa sin duplicar.
class MovementDetailPage extends StatefulWidget {
  const MovementDetailPage({
    super.key,
    required this.transactionId,
    this.service,
  });

  final String transactionId;
  final MovementsServiceBase? service;

  @override
  State<MovementDetailPage> createState() => _MovementDetailPageState();
}

class _MovementDetailPageState extends State<MovementDetailPage> {
  Future<MovementDetail>? _future;
  MovementDetail? _reversed;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<MovementDetail> _load() async {
    final service = widget.service;
    if (service == null) {
      throw ApiException(
        code: 'UNKNOWN',
        message: 'Servicio de movimientos no configurado.',
      );
    }
    return service.detail(widget.transactionId);
  }

  void _retry() {
    setState(() {
      _reversed = null;
      _future = _load();
    });
  }

  /// Detalle efectivo: tras un reverso 200 se exhibe `REVERSED` sin
  /// re-consultar (el servidor ya confirmo el estado terminal).
  MovementDetail _effective(MovementDetail loaded) {
    final rev = _reversed;
    if (rev == null) return loaded;
    if (rev.transactionId != loaded.transactionId) return loaded;
    return rev;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surfaceContainerLow,
      appBar: AppBar(
        backgroundColor: AppColors.surfaceContainerLow,
        title: const Text('Detalle de movimiento'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => context.canPop() ? context.pop() : null,
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: SizedBox(
              width: 48,
              height: 48,
              child: IconButton(
                key: const Key('movement-detail-avatar'),
                onPressed: () => context.push('/profile'),
                padding: EdgeInsets.zero,
                tooltip: 'Mi perfil',
                icon: const CircleAvatar(
                  radius: 16,
                  backgroundColor: AppColors.primary,
                  child: Icon(
                    Icons.person_outline,
                    size: 18,
                    color: Colors.white,
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
      body: FutureBuilder<MovementDetail>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingView(message: 'Cargando detalle...');
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            final message = error is ApiException
                ? movementUserMessage(error)
                : 'Ocurrió un error al consultar el servidor.';
            return Padding(
              padding: const EdgeInsets.all(16),
              child: ErrorView(message: message, onRetry: _retry),
            );
          }
          final detail = _effective(snapshot.data!);
          return _DetailBody(
            detail: detail,
            onReverse: detail.isReversible
                ? () => _confirmReverse(detail)
                : null,
            nonReversibleHint:
                detail.isReversible ? null : detail.nonReversibleHint,
            onDownload: () => _download(detail),
            onReload: _retry,
          );
        },
      ),
    );
  }

  Future<void> _download(MovementDetail detail) async {
    final service = widget.service;
    if (service == null) return;
    try {
      final receipt = await service.receipt(detail.transactionId);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            'Constancia ${receipt.suggestedFilename} '
            '(${receipt.bytes.length} bytes). El guardado nativo está pendiente.',
          ),
        ),
      );
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(movementUserMessage(e))),
      );
    }
  }

  Future<void> _confirmReverse(MovementDetail detail) async {
    final reasonController = TextEditingController();
    // Clave del intento: se genera una vez por dialogo y se reutiliza si el
    // usuario reintenta tras un fallo de red (regla 4 de `features/README`).
    final idempotencyKey = const Uuid().v4();
    final confirmed = await showDialog<String?>(
      context: context,
      builder: (context) => _ReverseDialog(controller: reasonController),
    );
    if (confirmed == null || !mounted) return;
    final service = widget.service;
    if (service == null) return;
    try {
      final result = await service.reverse(
        detail.transactionId,
        reason: confirmed.isEmpty ? null : confirmed,
        idempotencyKey: idempotencyKey,
      );
      if (!mounted) return;
      setState(() {
        _reversed = MovementDetail(
          transactionId: detail.transactionId,
          type: detail.type,
          status: result.status.isEmpty ? 'REVERSED' : result.status,
          amountMinor: detail.amountMinor,
          currency: detail.currency,
          feeMinor: detail.feeMinor,
          externalRef: detail.externalRef,
          tracking: detail.tracking,
          createdAt: detail.createdAt,
          settledAt: detail.settledAt,
          sourceAccountId: detail.sourceAccountId,
          targetAccountId: detail.targetAccountId,
        );
      });
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            result.replayed
                ? 'El reverso ya estaba registrado. Sin duplicados.'
                : 'Reverso registrado. Los fondos volverán a tu cuenta.',
          ),
        ),
      );
    } on ApiException catch (e) {
      if (!mounted) return;
      final message = e.code == 'CONFLICT' ||
              e.code == 'IDEMPOTENCY_CONFLICT' ||
              e.code == 'DUPLICATE_REQUEST'
          ? 'Esta operación ya fue reversada o está en conflicto. '
              'Recargando su estado actual.'
          : movementUserMessage(e);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(message)),
      );
      // Tras un 409 el estado pudo cambiar en el servidor: recargar.
      if (e.code == 'CONFLICT' ||
          e.code == 'IDEMPOTENCY_CONFLICT' ||
          e.code == 'DUPLICATE_REQUEST') {
        _retry();
      }
    }
  }
}

/// Dialogo de confirmacion del reverso (motivo opcional <= 280).
class _ReverseDialog extends StatefulWidget {
  const _ReverseDialog({required this.controller});

  final TextEditingController controller;

  @override
  State<_ReverseDialog> createState() => _ReverseDialogState();
}

class _ReverseDialogState extends State<_ReverseDialog> {
  @override
  void initState() {
    super.initState();
    widget.controller.addListener(() => setState(() {}));
  }

  @override
  Widget build(BuildContext context) {
    final length = widget.controller.text.characters.length;
    return AlertDialog(
      title: const Text('Solicitar reverso'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'El reverso devuelve los fondos a tu cuenta de origen. '
            'Puedes agregar un motivo opcional (máximo 280 caracteres).',
            style: TextStyle(fontSize: 13),
          ),
          const SizedBox(height: 12),
          TextField(
            key: const Key('reverse-reason'),
            controller: widget.controller,
            maxLength: 280,
            maxLines: 3,
            decoration: const InputDecoration(
              hintText: 'Motivo del reverso (opcional)',
              counterText: '',
            ),
          ),
          Align(
            alignment: Alignment.centerRight,
            child: Text(
              '$length/280',
              style: const TextStyle(fontSize: 11, color: AppColors.outline),
            ),
          ),
        ],
      ),
      actions: [
        TextButton(
          key: const Key('reverse-cancel'),
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancelar'),
        ),
        FilledButton(
          key: const Key('reverse-confirm'),
          onPressed: length <= 280
              ? () => Navigator.of(context).pop(widget.controller.text.trim())
              : null,
          child: const Text('Confirmar reverso'),
        ),
      ],
    );
  }
}

/// Cuerpo del detalle: hero + desglose + constancia + reverso.
class _DetailBody extends StatelessWidget {
  const _DetailBody({
    required this.detail,
    required this.onReverse,
    required this.nonReversibleHint,
    required this.onDownload,
    required this.onReload,
  });

  final MovementDetail detail;
  final VoidCallback? onReverse;
  final String? nonReversibleHint;
  final VoidCallback onDownload;
  final VoidCallback onReload;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        _HeroCard(detail: detail, onReload: onReload),
        const SizedBox(height: 12),
        _BreakdownCard(detail: detail),
        const SizedBox(height: 12),
        _ReceiptCard(onDownload: onDownload),
        const SizedBox(height: 12),
        _ReverseCard(
          detail: detail,
          onReverse: onReverse,
          hint: nonReversibleHint,
        ),
      ],
    );
  }
}

/// Hero (mockup `Hero`): contraparte, monto, estado, fecha y Op copiable.
class _HeroCard extends StatelessWidget {
  const _HeroCard({required this.detail, required this.onReload});

  final MovementDetail detail;
  final VoidCallback onReload;

  @override
  Widget build(BuildContext context) {
    final d = detail;
    final upper = d.status.toUpperCase();
    final inbound = d.amountMinor > 0;
    final sign = inbound ? '+' : '-';
    final op = (d.externalRef ?? '').isNotEmpty
        ? d.externalRef!
        : 'OP-${d.transactionId.length >= 8 ? d.transactionId.substring(0, 8).toUpperCase() : d.transactionId.toUpperCase()}';
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(16),
      ),
      child: Column(
        children: [
          Container(
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLow,
              borderRadius: BorderRadius.circular(12),
            ),
            child: Row(
              children: [
                Container(
                  width: 40,
                  height: 40,
                  alignment: Alignment.center,
                  decoration: const BoxDecoration(
                    color: AppColors.primaryContainer,
                    shape: BoxShape.circle,
                  ),
                  child: Text(
                    _initials(d),
                    style: const TextStyle(
                      fontWeight: FontWeight.bold,
                      color: Colors.white,
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        _counterparty(d),
                        style: const TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                          color: AppColors.primary,
                        ),
                      ),
                      Text(
                        _counterpartySub(d),
                        style: const TextStyle(
                          fontSize: 11,
                          color: AppColors.onSurfaceVariant,
                        ),
                      ),
                    ],
                  ),
                ),
                const Icon(
                  Icons.account_balance_outlined,
                  size: 18,
                  color: AppColors.primaryContainer,
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
          Container(
            width: 48,
            height: 48,
            decoration: const BoxDecoration(
              color: AppColors.surfaceContainerLow,
              shape: BoxShape.circle,
            ),
            child: Icon(
              inbound ? Icons.south_west : Icons.north_east,
              size: 26,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: 8),
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              // Auditoria m2: moneda real del detalle, nunca "PEN" fijo.
              Text(
                d.currency.toUpperCase(),
                style: const TextStyle(
                  fontWeight: FontWeight.w600,
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(width: 4),
              Text(
                '$sign ${formatMoneyMinor(d.amountMinor.abs(), d.currency)}',
                style: const TextStyle(
                  fontSize: 28,
                  fontWeight: FontWeight.bold,
                  color: AppColors.primary,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          _HeroStatusPill(status: d.status, isCce: _isCce(d)),
          const SizedBox(height: 8),
          Text(
            _dateLabel(d),
            style: const TextStyle(
              fontSize: 12,
              color: AppColors.onSurfaceVariant,
            ),
          ),
          const SizedBox(height: 8),
          Container(
            padding:
                const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLow,
              borderRadius: BorderRadius.circular(16),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                  'Op:',
                  style: TextStyle(fontSize: 11, color: AppColors.onSurfaceVariant),
                ),
                const SizedBox(width: 4),
                Flexible(
                  child: Text(
                    op,
                    style: const TextStyle(
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                      color: AppColors.primary,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                IconButton(
                  key: const Key('movement-copy-op'),
                  icon: const Icon(Icons.copy, size: 14),
                  color: AppColors.primary,
                  padding: EdgeInsets.zero,
                  constraints: const BoxConstraints(),
                  onPressed: () {
                    Clipboard.setData(ClipboardData(text: op));
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(
                        content: Text('Número de operación copiado.'),
                      ),
                    );
                  },
                ),
              ],
            ),
          ),
          if (upper == 'REVERSED') ...[
            const SizedBox(height: 12),
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: const Color(0xFFE4EFE6),
                borderRadius: BorderRadius.circular(12),
              ),
              child: const Row(
                children: [
                  Icon(Icons.check, size: 20, color: AppColors.success),
                  SizedBox(width: 8),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Operación Reversada con Éxito',
                          style: TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                            color: AppColors.primary,
                          ),
                        ),
                        Text(
                          'Fondos restituidos a tu cuenta de origen.',
                          style: TextStyle(
                            fontSize: 11,
                            color: AppColors.onSurfaceVariant,
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}

String _initials(MovementDetail d) {
  final masked = (d.targetMasked.trim().isEmpty ? d.sourceMasked : d.targetMasked)
      .replaceAll(RegExp(r'[^0-9A-Z]'), '');
  final tail = masked.length >= 2
      ? masked.substring(masked.length - 2)
      : masked.padRight(2, '•');
  return tail.toUpperCase();
}

String _counterparty(MovementDetail d) {
  final masked = d.targetMasked.trim().isEmpty ? d.sourceMasked : d.targetMasked;
  if (masked.trim().isNotEmpty && masked.trim() != '••••') {
    return 'Cuenta $masked';
  }
  return typeLabel(d.type);
}

/// Subtitulo de contraparte (auditoria m2): "Transferencia CCE" solo para
/// `INTERBANK_TRANSFER`; el resto muestra su descripcion real.
String _counterpartySub(MovementDetail d) {
  if (d.type.toUpperCase() == 'INTERBANK_TRANSFER') {
    return '${typeLabel(d.type)} · Transferencia CCE';
  }
  return typeLabel(d.type);
}

/// Etiqueta de riel/red (auditoria m2): "Interbancaria diferida" solo para
/// CCE; el resto describe su operacion real sin inventar red.
String _railLabel(String type) {
  switch (type.toUpperCase()) {
    case 'INTERBANK_TRANSFER':
      return 'Interbancaria diferida';
    case 'OWN_TRANSFER':
      return 'Entre cuentas propias';
    case 'THIRD_PARTY_TRANSFER':
      return 'Transferencia directa';
    case 'REVERSAL':
      return 'Reverso';
    default:
      return typeLabel(type);
  }
}

/// `true` si el detalle viaja por CCE (auditoria m2): solo entonces se
/// exhiben etiquetas/comisiones/tracking CCE.
bool _isCce(MovementDetail d) =>
    d.type.toUpperCase() == 'INTERBANK_TRANSFER';

/// Codigo de seguimiento CCE real (`tracking` del servidor); vacio si no hay.
/// No usa el fallback a `external_ref` (ese ya tiene su propia fila).
String _cceTrackingOf(MovementDetail d) {
  final tracking = d.tracking;
  for (final key in const ['cce_tracking', 'tracking_code']) {
    final value = tracking[key]?.toString().trim() ?? '';
    if (value.isNotEmpty) return value;
  }
  return '';
}

String _dateLabel(MovementDetail d) {
  final date = d.createdAt;
  if (date == null) return 'Fecha disponible en la constancia';
  const months = [
    'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
    'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre',
  ];
  final local = date.toLocal();
  final hh = local.hour.toString().padLeft(2, '0');
  final mm = local.minute.toString().padLeft(2, '0');
  final ss = local.second.toString().padLeft(2, '0');
  return '${local.day} de ${months[local.month - 1]} de ${local.year}, '
      '$hh:$mm:$ss GMT-5';
}

/// Pildora de estado del hero (mockups Reverso 1-4).
class _HeroStatusPill extends StatelessWidget {
  const _HeroStatusPill({required this.status, required this.isCce});

  final String status;

  /// Solo con CCE se rotula "Liquidado CCE"/"En Compensación".
  final bool isCce;

  @override
  Widget build(BuildContext context) {
    final upper = status.toUpperCase();
    late final Color bg;
    late final Color fg;
    late final Color dot;
    late final String label;
    switch (upper) {
      case 'SETTLED':
        bg = const Color(0xFFEAE8E3);
        fg = AppColors.primary;
        dot = AppColors.success;
        label = isCce ? 'SETTLED · Liquidado CCE' : 'SETTLED · Liquidado';
      case 'POSTED':
      case 'CONCILIATED':
        bg = const Color(0xFFEAE8E3);
        fg = AppColors.primary;
        dot = AppColors.success;
        label = '$upper · Confirmado';
      case 'PENDING':
      case 'PROCESSING':
      case 'PENDING_AUTHORIZATION':
        bg = AppColors.warningContainer;
        fg = AppColors.onWarningContainer;
        dot = AppColors.secondary;
        label = isCce ? 'PENDING · En Compensación' : 'PENDING · Pendiente';
      case 'REVERSED':
        bg = AppColors.surfaceContainerHighest;
        fg = AppColors.primary;
        dot = AppColors.primary;
        label = 'REVERSED · Reversado';
      case 'REJECTED':
      case 'FAILED':
        bg = AppColors.errorContainer;
        fg = AppColors.error;
        dot = AppColors.error;
        label = '$upper · No debitado';
      default:
        bg = AppColors.surfaceContainer;
        fg = AppColors.onSurfaceVariant;
        dot = AppColors.outline;
        label = statusLabel(status);
    }
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(color: dot, shape: BoxShape.circle),
          ),
          const SizedBox(width: 6),
          Text(
            label,
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: fg,
            ),
          ),
        ],
      ),
    );
  }
}

/// Desglose de la operacion (mockup `Desglose`, valores del servidor).
class _BreakdownCard extends StatelessWidget {
  const _BreakdownCard({required this.detail});

  final MovementDetail detail;

  @override
  Widget build(BuildContext context) {
    final d = detail;
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(16),
      ),
      child: Column(
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const Text(
                'DESGLOSE DE LA OPERACIÓN',
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                  color: AppColors.primary,
                ),
              ),
              // Auditoria m2: riel real segun `type` (CCE solo interbancario).
              Text(
                _railLabel(d.type),
                style: const TextStyle(
                  fontSize: 11,
                  color: AppColors.onSurfaceVariant,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          _Row(label: 'Cuenta de origen', value: 'Cuenta ${d.sourceMasked}'),
          _Row(label: 'Destinatario', value: 'Cuenta ${d.targetMasked}'),
          // Auditoria m2: sin fila DNI. El detalle (`GET /transactions/{id}`)
          // no trae documento del titular y mostrar un placeholder fijo para
          // todo tipo inventa datos.
          _Row(
            label: 'Tipo de operación',
            value: typeLabel(d.type),
          ),
          _Row(
            label: 'Núm. operación',
            value: (d.externalRef ?? '').isNotEmpty
                ? d.externalRef!
                : d.transactionId.length >= 8
                    ? d.transactionId.substring(0, 8).toUpperCase()
                    : d.transactionId,
          ),
          const Divider(color: AppColors.divider),
          _Row(
            label: 'Subtotal transferencia',
            value: formatMoneyMinor(
              d.amountMinor.abs(),
              d.currency,
            ),
          ),
          _Row(
            // Auditoria m2: "servicio CCE" solo para interbancarias.
            label: _isCce(d) ? 'Comisión servicio CCE' : 'Comisión del servicio',
            value: formatMoneyMinor(d.feeMinor, d.currency),
          ),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const Text(
                'Total debitado',
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                  color: AppColors.primary,
                ),
              ),
              Text(
                formatMoneyMinor(d.totalMinor, d.currency),
                style: AppTypography.titleMd.copyWith(fontSize: 15),
              ),
            ],
          ),
          const SizedBox(height: 8),
          // Auditoria m2: el tracking CCE solo existe para interbancarias
          // con `tracking` real del servidor (`cce_tracking`/`tracking_code`).
          // Sin el, no se rotula nada como CCE (el `external_ref` ya tiene su
          // propia fila "Núm. operación").
          if (_cceTrackingOf(d).isNotEmpty)
            Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              decoration: BoxDecoration(
                color: AppColors.surfaceContainerLow,
                borderRadius: BorderRadius.circular(8),
              ),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  const Text(
                    'Tracking CCE:',
                    style: TextStyle(
                      fontSize: 11,
                      color: AppColors.onSurfaceVariant,
                    ),
                  ),
                  Flexible(
                    child: Text(
                      _cceTrackingOf(d),
                      style: const TextStyle(
                        fontSize: 11,
                        fontWeight: FontWeight.w600,
                        color: AppColors.primary,
                      ),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(
            label,
            style: const TextStyle(
              fontSize: 12,
              color: AppColors.onSurfaceVariant,
            ),
          ),
          const SizedBox(width: 12),
          Flexible(
            child: Text(
              value,
              textAlign: TextAlign.right,
              style: const TextStyle(
                fontSize: 12,
                fontWeight: FontWeight.w600,
                color: AppColors.primary,
              ),
              overflow: TextOverflow.ellipsis,
            ),
          ),
        ],
      ),
    );
  }
}

/// Constancia oficial (mockup `Constancia`, E2-T10).
class _ReceiptCard extends StatelessWidget {
  const _ReceiptCard({required this.onDownload});

  final VoidCallback onDownload;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(16),
      ),
      child: Column(
        children: [
          Row(
            children: [
              Container(
                width: 40,
                height: 40,
                decoration: BoxDecoration(
                  color: AppColors.surfaceContainer,
                  borderRadius: BorderRadius.circular(10),
                ),
                child: const Icon(
                  Icons.receipt_long_outlined,
                  size: 20,
                  color: AppColors.primary,
                ),
              ),
              const SizedBox(width: 10),
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Constancia Oficial (E2-T10)',
                      style: TextStyle(
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                        color: AppColors.primary,
                      ),
                    ),
                    Text(
                      'Firma Digital SBS Certificada',
                      style: TextStyle(
                        fontSize: 11,
                        color: AppColors.onSurfaceVariant,
                      ),
                    ),
                  ],
                ),
              ),
              FilledButton.tonal(
                key: const Key('movement-download-receipt'),
                onPressed: onDownload,
                style: FilledButton.styleFrom(
                  backgroundColor: AppColors.surfaceContainer,
                  foregroundColor: AppColors.primary,
                  minimumSize: const Size(0, 30),
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  textStyle: const TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                child: const Text('Descargar'),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Container(
            padding:
                const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLow,
              borderRadius: BorderRadius.circular(8),
            ),
            child: const Row(
              children: [
                Icon(
                  Icons.verified_outlined,
                  size: 16,
                  color: AppColors.secondary,
                ),
                SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'Documento firmado digitalmente por la entidad.',
                    style: TextStyle(
                      fontSize: 10,
                      color: AppColors.onSurfaceVariant,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Gestion de reverso (mockups Reverso 1-4).
class _ReverseCard extends StatelessWidget {
  const _ReverseCard({
    required this.detail,
    required this.onReverse,
    required this.hint,
  });

  final MovementDetail detail;
  final VoidCallback? onReverse;
  final String? hint;

  @override
  Widget build(BuildContext context) {
    final enabled = onReverse != null;
    final upper = detail.status.toUpperCase();
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(16),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'GESTIÓN DE REVERSO BANCARIO',
            style: TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: 12),
          _ReverseNotice(detail: detail, hint: hint),
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            height: 52,
            child: FilledButton.icon(
              key: const Key('movement-reverse-action'),
              onPressed: enabled ? onReverse : null,
              icon: Icon(
                upper == 'REVERSED' ? Icons.check : Icons.history,
                size: 18,
              ),
              label: Text(
                _actionLabel(detail),
                style: const TextStyle(
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                ),
              ),
              style: FilledButton.styleFrom(
                backgroundColor: enabled
                    ? AppColors.primaryContainer
                    : AppColors.surfaceContainerHighest,
                foregroundColor:
                    enabled ? Colors.white : AppColors.outline,
                disabledBackgroundColor:
                    AppColors.surfaceContainerHighest,
                disabledForegroundColor: AppColors.outline,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
            ),
          ),
          const SizedBox(height: 8),
          Text(
            _caption(detail),
            textAlign: TextAlign.center,
            style: const TextStyle(
              fontSize: 11,
              color: AppColors.onSurfaceVariant,
            ),
          ),
        ],
      ),
    );
  }
}

class _ReverseNotice extends StatelessWidget {
  const _ReverseNotice({required this.detail, required this.hint});

  final MovementDetail detail;
  final String? hint;

  @override
  Widget build(BuildContext context) {
    final upper = detail.status.toUpperCase();
    late final Color bg;
    late final IconData icon;
    late final Color iconColor;
    late final String text;
    if (detail.isReversible) {
      bg = AppColors.surfaceContainerLow;
      icon = Icons.info_outline;
      iconColor = AppColors.primary;
      text = 'Los reversos inmediatos aplican dentro de las últimas 72h '
          'conforme al reglamento.';
    } else if (upper == 'PENDING' ||
        upper == 'PROCESSING' ||
        upper == 'PENDING_AUTHORIZATION') {
      bg = const Color(0xFFFFF4E0);
      icon = Icons.hourglass_empty;
      iconColor = AppColors.secondary;
      text = hint ??
          'Aún en compensación interbancaria y no admite reverso '
              'automático ahora.';
    } else if (upper == 'REVERSED') {
      bg = AppColors.surfaceContainerLow;
      icon = Icons.history;
      iconColor = AppColors.primary;
      text = 'Transacción totalmente anulada y devuelta a tu cuenta de origen.';
    } else if (detail.isExpired) {
      bg = AppColors.surfaceContainerLow;
      icon = Icons.block_outlined;
      iconColor = AppColors.error;
      text = hint ??
          'Plazo límite superado (máximo 72 horas bancarias). '
              'Contacta a soporte para reclamos.';
    } else {
      bg = AppColors.surfaceContainerLow;
      icon = Icons.info_outline;
      iconColor = AppColors.primary;
      text = hint ?? 'Este estado no admite reverso automático.';
    }
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          Icon(icon, size: 18, color: iconColor),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              text,
              style: const TextStyle(
                fontSize: 12,
                color: AppColors.onSurfaceVariant,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

String _actionLabel(MovementDetail detail) {
  if (detail.isReversible) return 'Solicitar reverso de transacción';
  switch (detail.status.toUpperCase()) {
    case 'REVERSED':
      return 'Operación Ya Reversada';
    case 'PENDING':
    case 'PROCESSING':
    case 'PENDING_AUTHORIZATION':
      return 'Reverso no disponible (En tránsito)';
    default:
      return detail.isExpired
          ? 'Plazo límite superado (> 72h)'
          : 'Reverso no disponible';
  }
}

String _caption(MovementDetail detail) {
  if (detail.isReversible) {
    // Auditoria m2: protocolo CCE solo para interbancarias.
    return _isCce(detail)
        ? 'Canalizado por protocolo CCE 24/7 sin penalidad.'
        : 'Reverso inmediato sin penalidad dentro de las 72 h.';
  }
  switch (detail.status.toUpperCase()) {
    case 'PENDING':
    case 'PROCESSING':
    case 'PENDING_AUTHORIZATION':
      return 'Espera la liquidación para solicitar reverso.';
    case 'REVERSED':
      return 'Sin acciones pendientes sobre esta operación.';
    default:
      return 'Si necesitas ayuda, contacta a Soporte CuyCash 24/7.';
  }
}
