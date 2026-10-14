import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/error_view.dart';
import '../data/transfers_service.dart';
import '../models/transfer.dart';
import 'widgets/cci_status_banner.dart';

/// Argumentos del seguimiento interbancario (viajan por `extra`).
class TrackingArgs {
  const TrackingArgs({
    required this.transactionId,
    this.holderMasked = '',
    this.amountMinor = 0,
    this.currency = 'PEN',
  });

  final String transactionId;
  final String holderMasked;
  final int amountMinor;
  final String currency;
}

enum _Phase { loading, polling, success, failed, pollError }

/// Seguimiento de la saga interbancaria (mockup `Procesando Transferencia`).
///
/// Tras el `202`, consulta `GET /transactions/{id}` cada 3 s hasta un estado
/// terminal. El reintento de seguimiento SOLO re-consulta (GET, no mueve
/// dinero): nunca se duplica el debito. El titular verificado se exhibe
/// enmascarado (banner verde CCI).
class TrackingPage extends StatefulWidget {
  const TrackingPage({super.key, required this.args, this.service});

  final TrackingArgs args;
  final TransfersServiceBase? service;

  @override
  State<TrackingPage> createState() => _TrackingPageState();
}

class _TrackingPageState extends State<TrackingPage> {
  static const _interval = Duration(seconds: 3);
  static const _maxAttempts = 40;

  Timer? _timer;
  _Phase _phase = _Phase.loading;
  TransactionDetail? _detail;
  ApiException? _error;
  int _attempts = 0;

  @override
  void initState() {
    super.initState();
    _fetch();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _fetch() async {
    final service = widget.service;
    if (service == null) {
      setState(() {
        _phase = _Phase.pollError;
        _error = ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de transferencias no configurado.',
        );
      });
      return;
    }
    setState(() {
      if (_phase != _Phase.polling) _phase = _Phase.loading;
      _error = null;
    });
    await _poll(service);
    if (!mounted) return;
    if (_phase == _Phase.polling) _startTimer(service);
  }

  void _startTimer(TransfersServiceBase service) {
    _timer?.cancel();
    _timer = Timer.periodic(_interval, (_) async {
      if (!mounted) return;
      if (_attempts >= _maxAttempts) {
        _timer?.cancel();
        setState(() {
          _phase = _Phase.pollError;
          _error = ApiException(
            code: 'TRANSFER_TIMEOUT',
            message: transferUserMessage(
              ApiException(code: 'TRANSFER_TIMEOUT', message: ''),
            ),
          );
        });
        return;
      }
      await _poll(service);
    });
  }

  Future<void> _poll(TransfersServiceBase service) async {
    try {
      final detail = await service.getTransaction(widget.args.transactionId);
      if (!mounted) return;
      _timer?.cancel();
      _Phase next;
      if (detail.isSuccess) {
        next = _Phase.success;
      } else if (detail.isTerminal) {
        next = _Phase.failed;
      } else {
        next = _Phase.polling;
      }
      setState(() {
        _attempts++;
        _detail = detail;
        _phase = next;
      });
      if (!mounted) return;
      if (_phase == _Phase.polling) {
        _timer?.cancel();
        _startTimer(service);
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      _timer?.cancel();
      setState(() {
        _phase = _Phase.pollError;
        _error = e;
      });
    }
  }

  /// Reintento de SEGUIMIENTO: solo vuelve a consultar (GET); el POST no se
  /// repite, asi que no hay riesgo de doble debito.
  void _retryPoll() {
    final service = widget.service;
    if (service == null) return;
    setState(() {
      _phase = _Phase.polling;
      _error = null;
    });
    _poll(service);
    if (mounted && _phase == _Phase.polling) _startTimer(service);
  }

  String get _holder =>
      _detail?.holderMasked.isNotEmpty == true
          ? _detail!.holderMasked
          : widget.args.holderMasked;

  @override
  Widget build(BuildContext context) {
    switch (_phase) {
      case _Phase.loading:
      case _Phase.polling:
        return _processingView();
      case _Phase.success:
        return _successView();
      case _Phase.failed:
        return _failedView();
      case _Phase.pollError:
        return _pollErrorView();
    }
  }

  Widget _processingView() {
    return Scaffold(
      backgroundColor: AppColors.primaryContainer,
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 48),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Container(
                  key: const Key('tracking-loading'),
                  width: 96,
                  height: 96,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(
                      color: AppColors.onPrimaryContainer,
                      width: 4,
                    ),
                  ),
                  child: const SizedBox.square(
                    dimension: 32,
                    child: CircularProgressIndicator(
                      strokeWidth: 3,
                      color: AppColors.onPrimary,
                    ),
                  ),
                ),
                const SizedBox(height: 24),
                Text(
                  'Procesando tu transferencia',
                  textAlign: TextAlign.center,
                  style: AppTypography.titleMd.copyWith(
                    fontSize: 22,
                    color: AppColors.onPrimary,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  'Esto toma solo unos segundos.',
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyLg.copyWith(
                    fontSize: 15,
                    color: AppColors.onPrimaryContainer,
                  ),
                ),
                const SizedBox(height: 24),
                // m6: indicador DINAMICO segun el estado real del
                // seguimiento (fase + nro. de consulta + ultimo estado
                // del servidor). Antes era una checklist fija
                // ("Validando fondos ✓ / Registrando… / Notificando…")
                // que nunca cambiaba: progreso falso. Ahora solo se
                // muestra lo que se sabe de verdad.
                Container(
                  width: 280,
                  padding: const EdgeInsets.all(20),
                  decoration: BoxDecoration(
                    color: AppColors.tertiaryContainer,
                    borderRadius: BorderRadius.circular(14),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          const SizedBox.square(
                            dimension: 20,
                            child: CircularProgressIndicator(
                              strokeWidth: 2.5,
                              color: AppColors.secondaryContainer,
                            ),
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: Text(
                              _trackingStatusLine(),
                              key: const Key('tracking-status-line'),
                              style: AppTypography.bodyMd.copyWith(
                                color: AppColors.onPrimary,
                              ),
                            ),
                          ),
                        ],
                      ),
                      if (_detail != null) ...[
                        const SizedBox(height: 8),
                        Text(
                          'Último estado: ${_statusLabel(_detail!.status)}',
                          style: AppTypography.bodyMd.copyWith(
                            fontSize: 12,
                            color: AppColors.onPrimaryContainer,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: 16),
                if (_attempts > 1)
                  Text(
                    'Consulta $_attempts de seguimiento a la CCE...',
                    style: AppTypography.bodyMd.copyWith(
                      fontSize: 12,
                      color: AppColors.onPrimaryContainer,
                    ),
                  ),
                const SizedBox(height: 8),
                Text(
                  'No cierres la aplicación.',
                  style: AppTypography.bodyMd.copyWith(
                    fontSize: 12,
                    color: AppColors.onPrimary.withValues(alpha: 0.5),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _successView() {
    final detail = _detail!;
    // Estado terminal: sin back (fisico ni flecha); la salida es
    // "Ver comprobante" o "Volver al inicio" (-> /home con `go`).
    return PopScope(
      canPop: false,
      child: Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        title: const Text('Seguimiento'),
        automaticallyImplyLeading: false,
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          AppCard(
            key: const Key('tracking-success'),
            padding: const EdgeInsets.all(20),
            child: Column(
              children: [
                Container(
                  width: 64,
                  height: 64,
                  alignment: Alignment.center,
                  decoration: const BoxDecoration(
                    color: AppColors.successContainer,
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(
                    Icons.check,
                    size: 32,
                    color: AppColors.success,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  'Transferencia registrada',
                  style: AppTypography.titleMd.copyWith(
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  formatMoneyMinor(detail.amountMinor, detail.currency),
                  style: AppTypography.displayLg.copyWith(
                    fontSize: 32,
                    color: AppColors.onSurface,
                  ),
                ),
                const SizedBox(height: 8),
                AppStatusChip.info(label: _statusLabel(detail.status)),
              ],
            ),
          ),
          const SizedBox(height: 12),
          if (_holder.isNotEmpty)
            CciStatusBanner(
              state: CciState.verified,
              holderMasked: _holder,
              accountLabel: 'Destino interbancario CCE',
            ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              key: const Key('tracking-receipt-cta'),
              onPressed:
                  () => context.pushReplacement(
                    '/transfers/receipt/${detail.transactionId}',
                  ),
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Ver comprobante'),
            ),
          ),
          const SizedBox(height: 8),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('tracking-home-cta'),
              onPressed: () => context.go('/home'),
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Volver al inicio'),
            ),
          ),
        ],
      ),
      ),
    );
  }

  Widget _failedView() {
    final detail = _detail!;
    final insufficient = detail.isInsufficientFunds;
    final timeout = detail.failureCode == 'TRANSFER_TIMEOUT' ||
        detail.failureCode == 'INTERBANK_UNAVAILABLE' ||
        detail.failureCode == 'INTERBANK_REJECTED';
    // Estado terminal (REJECTED/FAILED/timeout): sin back al
    // paso de confirmacion; la salida es reintentar (solo GET) o
    // "Volver al inicio" (-> /home con `go`).
    return PopScope(
      canPop: false,
      child: Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        title: const Text('Seguimiento'),
        automaticallyImplyLeading: false,
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          AppCard(
            key: const Key('tracking-failure'),
            padding: const EdgeInsets.all(20),
            child: Column(
              children: [
                Container(
                  width: 64,
                  height: 64,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: timeout
                        ? AppColors.warningContainer
                        : AppColors.errorContainer,
                    shape: BoxShape.circle,
                  ),
                  child: Icon(
                    timeout ? Icons.schedule : Icons.wallet_outlined,
                    size: 32,
                    color: timeout ? AppColors.warning : AppColors.error,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  insufficient
                      ? 'No tienes saldo suficiente'
                      : timeout
                          ? 'Sigue en seguimiento'
                          : 'La transferencia no prosperó',
                  textAlign: TextAlign.center,
                  style: AppTypography.titleMd.copyWith(
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  insufficient
                      ? 'Tu dinero está seguro. No se debitó nada.'
                      : transferUserMessage(
                        ApiException(
                          code: detail.failureCode ?? 'UNKNOWN',
                          message: '',
                        ),
                      ),
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.onSurfaceVariant,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 16),
          if (timeout) ...[
            SizedBox(
              width: double.infinity,
              child: FilledButton.icon(
                key: const Key('tracking-retry'),
                onPressed: _retryPoll,
                icon: const Icon(Icons.refresh, size: 18),
                label: const Text('Reintentar seguimiento'),
                style: FilledButton.styleFrom(
                  minimumSize: const Size(88, 52),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 8),
            Text(
              'El reintento solo consulta el estado: no duplica el débito.',
              textAlign: TextAlign.center,
              style: AppTypography.bodyMd.copyWith(
                fontSize: 12,
                color: AppColors.outline,
              ),
            ),
            const SizedBox(height: 8),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton(
                key: const Key('tracking-home-cta'),
                onPressed: () => context.go('/home'),
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(88, 52),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                ),
                child: const Text('Volver al inicio'),
              ),
            ),
          ] else ...[
            SizedBox(
              width: double.infinity,
              child: FilledButton(
                key: const Key('tracking-home-cta'),
                onPressed: () => context.go('/home'),
                style: FilledButton.styleFrom(
                  minimumSize: const Size(88, 52),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                ),
                child: const Text('Volver al inicio'),
              ),
            ),
          ],
        ],
      ),
      ),
    );
  }

  Widget _pollErrorView() {
    // M1: estado de error con reintento (solo GET) + salida "Volver"
    // (canPop ? pop : go('/transfers')): nadie queda atrapado si falla el
    // fetch de seguimiento.
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Seguimiento')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            ErrorView(
              message:
                  _error == null
                      ? 'No pudimos actualizar el estado.'
                      : transferUserMessage(_error!),
              onRetry: _retryPoll,
              retryLabel: 'Reintentar seguimiento',
            ),
            const SizedBox(height: 8),
            Text(
              'Si el dinero fue retenido, no se duplicará: el reintento '
              'solo consulta el estado.',
              textAlign: TextAlign.center,
              style: AppTypography.bodyMd.copyWith(
                fontSize: 12,
                color: AppColors.outline,
              ),
            ),
            const SizedBox(height: 16),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton(
                key: const Key('tracking-back-cta'),
                onPressed: () => context.canPop()
                    ? context.pop()
                    : context.go('/transfers'),
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(88, 52),
                ),
                child: const Text('Volver'),
              ),
            ),
          ],
        ),
      ),
    );
  }

  /// Linea honesta del seguimiento: fase real + nro. de consulta.
  ///
  /// m6: nunca inventa sub-pasos ("validando fondos", "notificando"):
  /// solo refleja si aun no hubo consulta, cuantas van y que el
  /// reintento es solo lectura (GET, sin doble debito).
  String _trackingStatusLine() {
    if (_phase == _Phase.loading && _attempts == 0) {
      return 'Iniciando el seguimiento…';
    }
    return 'Consultando el estado… (consulta $_attempts)';
  }

  String _statusLabel(String status) {
    switch (status.toUpperCase()) {
      case 'SETTLED':
        return 'COMPLETADA';
      case 'POSTED':
        return 'PUBLICADA';
      case 'FUNDS_HELD':
        return 'FONDOS RETENIDOS';
      case 'REJECTED':
        return 'RECHAZADA';
      case 'FAILED':
        return 'FALLIDA';
      default:
        return status.toUpperCase();
    }
  }
}
