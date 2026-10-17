import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../data/wallet_service.dart';
import '../models/wallet.dart';

/// Billetera del titular (HU13, E4-T04; sin mockup en `pantallas.fig` —
/// fila pendiente `billetera-vinculacion.png`, G71; UI desde tokens
/// `docs/20`).
///
/// - Lista los vinculos tal cual del backend (enmascarados; cliente
///   delgado: sin saldos, limites ni elegibilidad calculados).
/// - Entrada al flujo de vinculacion (`/wallet/link`); tras un alta
///   exitosa recarga la lista.
/// - Desvincular por vinculo activo (`DELETE /wallet/links/{id}` con
///   confirmacion; baja logica a `INACTIVE`): tras el `200` recarga la
///   lista y avisa; `404` neutro tambien recarga (el vinculo ya no esta
///   activo); error de red con Reintentar.
///
/// Estados (docs/20 §7): contenido, skeleton (carga), vacio, error +
/// Reintentar. Mensajes en espanol.
class WalletPage extends StatefulWidget {
  const WalletPage({super.key, this.service});

  final WalletServiceBase? service;

  @override
  State<WalletPage> createState() => _WalletPageState();
}

class _WalletPageState extends State<WalletPage> {
  Future<Wallet>? _future;

  /// Id del vinculo en curso de baja (inhabilita su boton; sin PAN/tokens
  /// en ningun mensaje).
  String? _unlinkingId;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<Wallet> _load() {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de billetera no configurado.',
        ),
      );
    }
    return service.getWallet();
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

  Future<void> _openLink() async {
    final linked = await context.push<bool>('/wallet/link');
    if (linked == true && mounted) _retry();
  }

  /// Entrada al area comercio (E4-T09, HU14): generar y ver cobros QR.
  void _openMerchantQr() {
    context.push('/wallet/merchant-qr');
  }

  /// Entrada al QR personal estatico (E4-T21, HU14): generar y mostrar
  /// el QR reutilizable del titular (sin monto ni caducidad).
  void _openPersonalQr() {
    context.push('/wallet/personal-qr');
  }

  /// Entrada al pago QR del pagador (E4-T13, HU15): escaner -> resumen ->
  /// confirmacion -> resultado.
  void _openQrPay() {
    context.push('/wallet/pay-qr');
  }

  /// Entrada a servicios (descubribilidad sprint-3): catálogo/búsqueda de
  /// empresas (→ `/wallet/services`, ruta real, sin "Próximamente").
  void _openServices() {
    context.push('/wallet/services');
  }

  /// Entrada a recargas (→ `/wallet/topups`, se mantiene junto a
  /// servicios para no perderla).
  void _openTopups() {
    context.push('/wallet/topups');
  }

  /// Baja logica del vinculo con confirmacion previa.
  Future<void> _confirmUnlink(WalletLink link) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => _UnlinkConfirmDialog(link: link),
    );
    if (confirmed != true || !mounted) return;
    await _performUnlink(link);
  }

  /// Ejecuta el `DELETE` y refleja lo que responde el servidor (cliente
  /// delgado: recarga la lista; `404` neutro tambien recarga).
  Future<void> _performUnlink(WalletLink link) async {
    final service = widget.service;
    if (service == null || _unlinkingId != null) return;
    setState(() => _unlinkingId = link.id);
    try {
      await service.unlink(link.id);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Fuente desvinculada.')),
      );
      _retry();
    } on ApiException catch (e) {
      if (!mounted) return;
      if (e.code == 'NOT_FOUND' || e.statusCode == 404) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text(
              'Ese vínculo ya no está activo. Actualizamos tu lista.',
            ),
          ),
        );
        _retry();
      } else if (e.isNetworkError) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(e.message),
            action: SnackBarAction(
              label: 'Reintentar',
              onPressed: () => _performUnlink(link),
            ),
          ),
        );
      } else {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(e.message)),
        );
      }
    } finally {
      if (mounted) {
        setState(() => _unlinkingId = null);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        title: const Text('Billetera'),
        actions: [
          TextButton.icon(
            key: const Key('wallet-link-action'),
            onPressed: _openLink,
            icon: const Icon(Icons.add, size: 20),
            label: const Text('Vincular'),
          ),
        ],
      ),
      body: FutureBuilder<Wallet>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const _WalletSkeleton();
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
          final wallet = snapshot.data ?? Wallet.empty;
          final links = wallet.links;
          // Rol de comercio del servidor (E4-T23): las cuentas personales
          // (`!isMerchant`, fail-closed ante error/red) no ven ninguna
          // entrada de comercio. "Mi QR personal" (E4-T21) sigue visible.
          final isMerchant = wallet.isMerchant;
          if (links.isEmpty) {
            return RefreshIndicator(
              onRefresh: _refresh,
              child: ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                children: [
                  const SizedBox(height: 48),
                  EmptyView(
                    message: 'Aún no tienes métodos vinculados\n'
                        'Vincula una cuenta del banco o una tarjeta para '
                        'pagar en un toque.',
                    actionLabel: 'Vincular fuente',
                    onAction: _openLink,
                  ),
                  Center(
                    child: TextButton.icon(
                      key: const Key('wallet-personal-qr-empty'),
                      onPressed: _openPersonalQr,
                      icon: const Icon(Icons.person_outline, size: 20),
                      label: const Text('Mi QR personal'),
                    ),
                  ),
                  // Solo comercio (E4-T23): oculto cuando `!isMerchant`.
                  if (isMerchant)
                    Center(
                      child: TextButton.icon(
                        key: const Key('wallet-merchant-qr-empty'),
                        onPressed: _openMerchantQr,
                        icon: const Icon(Icons.qr_code_2, size: 20),
                        label: const Text(
                          'Tengo un comercio: generar cobro QR',
                        ),
                      ),
                    ),
                  Center(
                    child: TextButton.icon(
                      key: const Key('wallet-pay-qr-empty'),
                      onPressed: _openQrPay,
                      icon: const Icon(Icons.qr_code_scanner, size: 20),
                      label: const Text('Pagar con QR'),
                    ),
                  ),
                  Center(
                    child: TextButton.icon(
                      key: const Key('wallet-services-empty'),
                      onPressed: _openServices,
                      icon: const Icon(Icons.receipt_long_outlined, size: 20),
                      label: const Text('Pagar servicios'),
                    ),
                  ),
                  Center(
                    child: TextButton.icon(
                      key: const Key('wallet-topups-empty'),
                      onPressed: _openTopups,
                      icon: const Icon(Icons.smartphone_outlined, size: 20),
                      label: const Text('Recargar'),
                    ),
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
              itemCount: links.length + 1,
              itemBuilder: (context, index) {
                if (index == 0) {
                  return Padding(
                    padding: const EdgeInsets.only(bottom: 12),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          'Tus métodos vinculados se muestran enmascarados. '
                          'El detalle completo nunca sale del servidor.',
                          style: AppTypography.bodyMd.copyWith(
                            fontSize: 12,
                            color: AppColors.secondaryText,
                          ),
                        ),
                        const SizedBox(height: 8),
                        SizedBox(
                          width: double.infinity,
                          child: OutlinedButton.icon(
                            key: const Key('wallet-personal-qr-action'),
                            onPressed: _openPersonalQr,
                            icon: const Icon(Icons.person_outline, size: 20),
                            label: const Text('Mi QR personal'),
                          ),
                        ),
                        const SizedBox(height: 8),
                        // Solo comercio (E4-T23): oculto cuando `!isMerchant`.
                        if (isMerchant)
                          SizedBox(
                            width: double.infinity,
                            child: OutlinedButton.icon(
                              key: const Key('wallet-merchant-qr-action'),
                              onPressed: _openMerchantQr,
                              icon: const Icon(Icons.qr_code_2, size: 20),
                              label: const Text('Cobrar como comercio'),
                            ),
                          ),
                        if (isMerchant) const SizedBox(height: 8),
                        const SizedBox(height: 8),
                        SizedBox(
                          width: double.infinity,
                          child: FilledButton.icon(
                            key: const Key('wallet-pay-qr-action'),
                            onPressed: _openQrPay,
                            icon: const Icon(Icons.qr_code_scanner, size: 20),
                            label: const Text('Pagar con QR'),
                          ),
                        ),
                        const SizedBox(height: 8),
                        SizedBox(
                          width: double.infinity,
                          child: OutlinedButton.icon(
                            key: const Key('wallet-services-action'),
                            onPressed: _openServices,
                            icon: const Icon(
                              Icons.receipt_long_outlined,
                              size: 20,
                            ),
                            label: const Text('Pagar servicios'),
                          ),
                        ),
                        const SizedBox(height: 8),
                        SizedBox(
                          width: double.infinity,
                          child: OutlinedButton.icon(
                            key: const Key('wallet-topups-action'),
                            onPressed: _openTopups,
                            icon: const Icon(
                              Icons.smartphone_outlined,
                              size: 20,
                            ),
                            label: const Text('Recargar'),
                          ),
                        ),
                      ],
                    ),
                  );
                }
                return Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: _WalletLinkCard(
                    link: links[index - 1],
                    unlinking: _unlinkingId == links[index - 1].id,
                    onUnlink: _confirmUnlink,
                  ),
                );
              },
            ),
          );
        },
      ),
    );
  }
}

/// Tarjeta de vinculo enmascarado (mockup pendiente; docs/20 §6).
///
/// Muestra `maskedLabel` (tal cual del backend) + chip de estado
/// (`success-container` para `ACTIVE`, `warning-container` para el
/// resto). Nunca muestra ni reconstruye el numero completo.
class _WalletLinkCard extends StatelessWidget {
  const _WalletLinkCard({
    required this.link,
    required this.onUnlink,
    this.unlinking = false,
  });

  final WalletLink link;
  final ValueChanged<WalletLink> onUnlink;
  final bool unlinking;

  @override
  Widget build(BuildContext context) {
    final icon =
        link.isAccount ? Icons.account_balance_outlined : Icons.credit_card;
    return AppCard(
      key: Key('wallet-link-${link.id}'),
      padding: const EdgeInsets.all(14),
      child: Row(
        children: [
          Container(
            width: 44,
            height: 44,
            alignment: Alignment.center,
            decoration: const BoxDecoration(
              shape: BoxShape.circle,
              color: AppColors.surfaceContainerHigh,
            ),
            child: Icon(icon, color: AppColors.primary, size: 24),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  link.maskedLabel,
                  style: AppTypography.titleMd.copyWith(fontSize: 15),
                ),
                const SizedBox(height: 6),
                if (link.isActive)
                  const AppStatusChip.success(label: 'Activa')
                else
                  AppStatusChip.warning(label: _statusLabel(link.status)),
                if (link.isActive) ...[
                  const SizedBox(height: 4),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: unlinking
                        ? const SizedBox.square(
                            dimension: 20,
                            child: CircularProgressIndicator(strokeWidth: 2.5),
                          )
                        : TextButton(
                            key: Key('wallet-unlink-${link.id}'),
                            style: TextButton.styleFrom(
                              foregroundColor: AppColors.error,
                              padding: const EdgeInsets.symmetric(
                                horizontal: 8,
                              ),
                              minimumSize: const Size(0, 36),
                              tapTargetSize:
                                  MaterialTapTargetSize.shrinkWrap,
                            ),
                            onPressed: () => onUnlink(link),
                            child: const Text('Desvincular'),
                          ),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Confirmacion de baja logica (solo dato enmascarado, sin PAN/tokens).
class _UnlinkConfirmDialog extends StatelessWidget {
  const _UnlinkConfirmDialog({required this.link});

  final WalletLink link;

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      title: const Text('¿Desvincular fuente?'),
      content: Text(
        'Se desvinculará ${link.maskedLabel} de tu billetera. '
        'Podrás volver a vincularla cuando quieras.',
      ),
      actions: [
        TextButton(
          key: const Key('unlink-cancel'),
          onPressed: () => Navigator.of(context).pop(false),
          child: const Text('Cancelar'),
        ),
        FilledButton(
          key: const Key('unlink-confirm'),
          style: FilledButton.styleFrom(backgroundColor: AppColors.error),
          onPressed: () => Navigator.of(context).pop(true),
          child: const Text('Sí, desvincular'),
        ),
      ],
    );
  }
}

String _statusLabel(String status) {
  switch (status.toUpperCase()) {
    case 'INACTIVE':
      return 'Inactiva';
    default:
      return status.isEmpty ? 'Sin estado' : status;
  }
}

/// Skeleton de carga (barras `#E4E2DD`, patron de `beneficiaries`).
class _WalletSkeleton extends StatelessWidget {
  const _WalletSkeleton();

  @override
  Widget build(BuildContext context) {
    return ListView(
      key: const Key('wallet-skeleton'),
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
