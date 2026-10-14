import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/app_text_field.dart';
import '../../../core/widgets/empty_view.dart';
import '../data/merchant_qr_service.dart';
import '../models/merchant_qr.dart';
import 'qr_payload_view.dart';

/// Pantalla del comercio para generar un cobro QR (HU14 CA-01, E4-T09;
/// sin mockup en `pantallas.fig` — fila pendiente
/// `billetera-qr-comercio.png`, G71; UI desde tokens `docs/20`).
///
/// - **Generar:** formulario de monto (soles) + concepto + moneda + modo
///   (`DYNAMIC`/`STATIC`) -> `POST /merchants/qr` (`Idempotency-Key`
///   opcional: se genera una vez por intento y el Reintentar reutiliza la
///   misma clave, sin duplicar el cargo).
/// - **Mostrar:** el QR se renderiza a partir del `qr_content` del
///   backend (sobre `BO-QR1`, `qr_flutter`) con su monto y su
///   `expires_at` (temporizador meramente visual: la
///   fuente de verdad es el `status` del servidor).
/// - **Estado:** boton `Actualizar estado` (`GET /merchants/qr/{id}`)
///   para reflejar el abono + entrada al detalle.
/// - Cliente delgado estricto (docs/19 §4): muestra `qr_content`/
///   `status`/`expires_at`/`amount_minor` tal cual del backend; no
///   firma, no calcula montos/estados ni decide el cobro.
///
/// Estados (docs/20 §7): formulario (contenido), generando (carga),
/// vacio (sin cobros aun), error accionable en espanol + Reintentar.
class MerchantQrPage extends StatefulWidget {
  const MerchantQrPage({super.key, this.service});

  final MerchantQrServiceBase? service;

  @override
  State<MerchantQrPage> createState() => _MerchantQrPageState();
}

class _MerchantQrPageState extends State<MerchantQrPage> {
  final _amountController = TextEditingController();
  final _conceptController = TextEditingController();
  String _currency = 'PEN';
  String _mode = 'DYNAMIC';

  /// Clave del intento vigente: se genera una vez y se reutiliza en el
  /// Reintentar del mismo intento (sin duplicar). "Generar otro" inicia
  /// un intento nuevo con clave nueva.
  String _idempotencyKey = const Uuid().v4();

  bool _saving = false;
  String? _formError;
  QrCharge? _created;

  /// Estado refrescado del ultimo cobro (`GET`, fuente de verdad para el
  /// badge tras el abono; el temporizador local nunca lo decide).
  QrChargeStatus? _refreshed;
  bool _refreshing = false;

  @override
  void dispose() {
    _amountController.dispose();
    _conceptController.dispose();
    super.dispose();
  }

  /// Badge del cobro: usa el `status` refrescado del servidor si existe,
  /// si no el del `POST` (ambos del backend; nunca calculado).
  String get _effectiveStatus =>
      _refreshed?.status ?? _created?.status ?? '';

  String _statusLabel(String status) {
    switch (status) {
      case 'ACTIVE':
        return 'Pendiente';
      case 'PAID':
        return 'Pagado';
      case 'EXPIRED':
        return 'Vencido';
      default:
        return status.isEmpty ? 'Sin estado' : status;
    }
  }

  Widget _statusChip(String status) {
    switch (status) {
      case 'PAID':
        return const AppStatusChip.success(label: 'Pagado');
      case 'EXPIRED':
        return const AppStatusChip.error(label: 'Vencido');
      case 'ACTIVE':
        return const AppStatusChip.warning(label: 'Pendiente');
      default:
        return AppStatusChip.info(label: _statusLabel(status));
    }
  }

  Future<void> _generate() async {
    if (_saving) return;
    final minor = parseSolesToMinor(_amountController.text);
    if ((_amountController.text.trim().isEmpty)) {
      setState(() => _formError = 'Ingresa el monto del cobro.');
      return;
    }
    if (minor == null) {
      setState(
        () => _formError = 'Ingresa un monto válido (p. ej. 25.50).',
      );
      return;
    }
    if (minor <= 0) {
      setState(() => _formError = 'El monto debe ser mayor a cero.');
      return;
    }
    if (_conceptController.text.length > 120) {
      setState(
        () => _formError = 'El concepto no puede superar 120 caracteres.',
      );
      return;
    }
    final service = widget.service;
    if (service == null) {
      setState(() => _formError = 'Servicio de cobros no configurado.');
      return;
    }
    setState(() {
      _saving = true;
      _formError = null;
    });
    try {
      final concept = _conceptController.text.trim();
      final charge = await service.createCharge(
        QrChargeInput(
          amountMinor: minor,
          currency: _currency,
          concept: concept.isEmpty ? null : concept,
          mode: _mode,
        ),
        idempotencyKey: _idempotencyKey,
      );
      if (!mounted) return;
      setState(() {
        _created = charge;
        _refreshed = null;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        // El Reintentar conserva `_idempotencyKey` (mismo intento).
        _formError = _friendlyCreateError(e.code, e.message);
      });
    } finally {
      if (mounted) {
        setState(() => _saving = false);
      }
    }
  }

  /// Refresca el estado del cobro contra el servidor (fuente de verdad
  /// para reflejar el abono, HU14 CA-04 parte).
  Future<void> _refreshStatus() async {
    final service = widget.service;
    final created = _created;
    if (service == null || created == null || _refreshing) return;
    setState(() {
      _refreshing = true;
      _formError = null;
    });
    try {
      final status = await service.getChargeStatus(created.chargeId);
      if (!mounted) return;
      setState(() => _refreshed = status);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _formError = _friendlyStatusError(e.code, e.message);
      });
    } finally {
      if (mounted) {
        setState(() => _refreshing = false);
      }
    }
  }

  void _generateAnother() {
    setState(() {
      _created = null;
      _refreshed = null;
      _formError = null;
      _amountController.clear();
      _conceptController.clear();
      _idempotencyKey = const Uuid().v4();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Cobrar con QR')),
      body: RefreshIndicator(
        onRefresh: () =>
            _created == null ? Future<void>.value() : _refreshStatus(),
        child: SingleChildScrollView(
          key: const Key('merchant-qr-scroll'),
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'Ingresa el monto y el concepto: generamos un QR firmado '
                'con su vigencia. El estado lo confirma el servidor.',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: 12),
              AppCard(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    AppTextField(
                      key: const Key('merchant-qr-amount'),
                      label: 'Monto (soles)',
                      controller: _amountController,
                      hintText: '25.50',
                      keyboardType: const TextInputType.numberWithOptions(
                        decimal: true,
                      ),
                      textInputAction: TextInputAction.next,
                      onChanged: (_) {
                        if (_formError != null) {
                          setState(() => _formError = null);
                        }
                      },
                    ),
                    const SizedBox(height: 12),
                    AppTextField(
                      key: const Key('merchant-qr-concept'),
                      label: 'Concepto (opcional)',
                      controller: _conceptController,
                      hintText: 'Café de la mañana',
                      textInputAction: TextInputAction.done,
                      onChanged: (_) {
                        if (_formError != null) {
                          setState(() => _formError = null);
                        }
                      },
                    ),
                    const SizedBox(height: 12),
                    DropdownButtonFormField<String>(
                      key: const Key('merchant-qr-currency-field'),
                      initialValue: _currency,
                      decoration: InputDecoration(
                        labelText: 'Moneda',
                        filled: true,
                        fillColor: AppColors.surfaceContainerLowest,
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(12),
                        ),
                      ),
                      items: const [
                        DropdownMenuItem(value: 'PEN', child: Text('PEN')),
                        DropdownMenuItem(value: 'USD', child: Text('USD')),
                      ],
                      onChanged: (value) => setState(() {
                        _currency = value ?? 'PEN';
                        _formError = null;
                      }),
                    ),
                    const SizedBox(height: 12),
                    SegmentedButton<String>(
                      segments: const [
                        ButtonSegment(
                          value: 'DYNAMIC',
                          label: Text('Dinámico'),
                        ),
                        ButtonSegment(
                          value: 'STATIC',
                          label: Text('Estático'),
                        ),
                      ],
                      selected: {_mode},
                      onSelectionChanged: (selected) => setState(() {
                        _mode = selected.single;
                        _formError = null;
                      }),
                    ),
                    if (_formError != null) ...[
                      const SizedBox(height: 12),
                      Container(
                        key: const Key('merchant-qr-error'),
                        width: double.infinity,
                        padding: const EdgeInsets.all(12),
                        decoration: BoxDecoration(
                          color: AppColors.errorContainer,
                          borderRadius: BorderRadius.circular(12),
                        ),
                        child: Text(
                          _formError!,
                          style: AppTypography.labelMd.copyWith(
                            fontSize: 13,
                            color: AppColors.onErrorContainer,
                          ),
                        ),
                      ),
                    ],
                    const SizedBox(height: 12),
                    SizedBox(
                      width: double.infinity,
                      height: 52,
                      child: FilledButton.icon(
                        key: const Key('merchant-qr-generate'),
                        onPressed: _saving ? null : _generate,
                        icon: _saving
                            ? const SizedBox.square(
                                dimension: 20,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2.5,
                                ),
                              )
                            : const Icon(Icons.qr_code_2, size: 20),
                        label: const Text(
                          'Generar QR',
                          style: TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),
              _ResultSection(
                saving: _saving,
                created: _created,
                refreshed: _refreshed,
                refreshing: _refreshing,
                statusChip: _statusChip(_effectiveStatus),
                onRefresh: _refreshStatus,
                onDetail: _created == null
                    ? null
                    : () => context.push(
                          '/wallet/merchant-charges/${_created!.chargeId}',
                        ),
                onGenerateAnother: _generateAnother,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Seccion del ultimo cobro: carga / vacio / contenido (QR + vigencia).
class _ResultSection extends StatelessWidget {
  const _ResultSection({
    required this.saving,
    required this.created,
    required this.refreshed,
    required this.refreshing,
    required this.statusChip,
    required this.onRefresh,
    required this.onDetail,
    required this.onGenerateAnother,
  });

  final bool saving;
  final QrCharge? created;
  final QrChargeStatus? refreshed;
  final bool refreshing;
  final Widget statusChip;
  final VoidCallback onRefresh;
  final VoidCallback? onDetail;
  final VoidCallback onGenerateAnother;

  @override
  Widget build(BuildContext context) {
    if (saving && created == null) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 24),
          child: CircularProgressIndicator(
            key: Key('merchant-qr-loading'),
          ),
        ),
      );
    }
    final charge = created;
    if (charge == null) {
      return const EmptyView(
        message: 'Aún no has generado ningún cobro.\n'
            'Completa el monto y pulsa Generar QR.',
      );
    }
    // Monto y vigencia tal cual del backend (cliente delgado).
    final amountMinor = refreshed?.amountMinor ?? charge.amountMinor;
    final currency = refreshed?.currency ?? charge.currency;
    final expiresAt = refreshed?.expiresAt ?? charge.expiresAt;
    return AppCard(
      key: const Key('merchant-qr-result'),
      padding: const EdgeInsets.all(20),
      child: Column(
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  'QR de cobro',
                  style: AppTypography.titleMd.copyWith(fontSize: 18),
                ),
              ),
              statusChip,
            ],
          ),
          const SizedBox(height: 4),
          Text(
            formatMinor(amountMinor, currency),
            key: const Key('merchant-qr-amount-display'),
            style: AppTypography.displayLg.copyWith(
              color: AppColors.primary,
            ),
          ),
          if ((charge.concept ?? '').isNotEmpty) ...[
            const SizedBox(height: 4),
            Text(
              charge.concept!,
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
            ),
          ],
          const SizedBox(height: 12),
          QrPayloadView(qrContent: charge.qrContent),
          const SizedBox(height: 12),
          Text(
            expiresAt == null || expiresAt.isEmpty
                ? 'Sin vencimiento (cobro estático)'
                : 'Vence: ${formatIsoLocal(expiresAt)}',
            key: const Key('merchant-qr-expires'),
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurfaceVariant,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            remainingText(expiresAt),
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            height: 48,
            child: FilledButton.icon(
              key: const Key('merchant-qr-refresh'),
              onPressed: refreshing ? null : onRefresh,
              icon: refreshing
                  ? const SizedBox.square(
                      dimension: 18,
                      child: CircularProgressIndicator(strokeWidth: 2.5),
                    )
                  : const Icon(Icons.refresh, size: 20),
              label: const Text('Actualizar estado'),
            ),
          ),
          const SizedBox(height: 8),
          SizedBox(
            width: double.infinity,
            height: 48,
            child: OutlinedButton(
              key: const Key('merchant-qr-detail'),
              onPressed: onDetail,
              child: const Text('Ver detalle del cobro'),
            ),
          ),
          TextButton(
            key: const Key('merchant-qr-new'),
            onPressed: onGenerateAnother,
            child: const Text('Generar otro cobro'),
          ),
        ],
      ),
    );
  }
}

/// Mensajes accionables por codigo de negocio al generar (sin tocar
/// `core`, fuera de frontera).
String _friendlyCreateError(String code, String fallback) {
  switch (code) {
    case 'VALIDATION_ERROR':
      return 'Revisa el monto y el concepto e inténtalo de nuevo.';
    case 'NOT_AUTHORIZED':
      return 'Tu cuenta no tiene rol de comercio. Contacta a tu banco.';
    case 'IDEMPOTENCY_CONFLICT':
      return 'Ese cobro ya fue registrado con otros datos. '
          'Revisa el detalle.';
    case 'RATE_LIMITED':
      return 'Demasiados intentos. Espera un momento e inténtalo de nuevo.';
    default:
      return fallback;
  }
}

/// Mensajes accionables por codigo de negocio al consultar estado.
String _friendlyStatusError(String code, String fallback) {
  switch (code) {
    case 'NOT_FOUND':
      return 'No encontramos ese cobro. Verifica el identificador.';
    case 'NOT_AUTHORIZED':
      return 'Tu cuenta no tiene rol de comercio. Contacta a tu banco.';
    default:
      return fallback;
  }
}
