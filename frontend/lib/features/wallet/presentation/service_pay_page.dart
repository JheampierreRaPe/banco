import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/empty_view.dart';
import '../../payment_authorization/data/payment_authorization_service.dart';
import '../../payment_authorization/payment_authorization_controller.dart';
import '../data/services_service.dart';
import '../data/wallet_service.dart';
import '../models/services.dart';
import '../models/wallet.dart';
import 'service_receipt_card.dart';

/// Pago de servicio con comprobante (E4-T19, HU16 CA-01/CA-04).
///
/// Flujo (cliente delgado estricto, docs/19 §4):
/// 1. Elige el `biller` (viene por `extra` desde el catalogo) + captura
///    `supply_ref` + monto + fuente de fondos (`GET /wallet`, solo
///    vinculos `ACCOUNT` activos, enmascarados).
/// 2. Confirma (`POST /services/payments` + `Idempotency-Key` del
///    intento; el Reintentar reutiliza la misma clave, sin duplicar):
///    - `SETTLED` -> [ServiceReceiptCard] con el `receipt` del servidor.
///    - `PENDING_AUTHORIZATION` (sobre el limite) -> biometria local o
///      PIN (`POST /transactions/{id}/authorize`, patron F-T03) y luego
///      replay con la MISMA clave para finalizar (patron HU15).
///    - `REJECTED` (`failure_code`) o error HTTP -> mensaje accionable
///      en español, sin descontar fondos.
///
/// Estados (docs/20 §7): contenido / carga / vacio / error con
/// Reintentar; mensajes en español; sin logica offline.
class ServicePayPage extends StatefulWidget {
  const ServicePayPage({
    super.key,
    this.biller,
    this.servicesService,
    this.walletService,
    this.authController,
    this.authService,
    this.onDone,
  });

  /// `Biller` elegido en el catalogo (campos `required_fields`
  /// dinamicos; nunca se suponen).
  final Biller? biller;
  final ServicesServiceBase? servicesService;
  final WalletServiceBase? walletService;
  final PaymentAuthorizationController? authController;
  final PaymentAuthorizationServiceBase? authService;
  final VoidCallback? onDone;

  @override
  State<ServicePayPage> createState() => _ServicePayPageState();
}

enum _Stage { loading, form, paying, pendingAuth, receipt, rejected, failed }

class _ServicePayPageState extends State<ServicePayPage> {
  final String _payKey = const Uuid().v4();

  _Stage _stage = _Stage.loading;
  List<WalletLink> _links = [];
  String? _selectedLinkId;
  String? _linksError;

  final TextEditingController _supply = TextEditingController();
  final TextEditingController _amount = TextEditingController();
  String? _formError;

  ServiceOperationResult? _result;
  ServiceFailure? _failure;
  String? _authMessage;
  bool _showPin = false;
  bool _authorizing = false;
  final TextEditingController _pin = TextEditingController();
  String? _pinError;

  @override
  void initState() {
    super.initState();
    _loadLinks();
  }

  @override
  void dispose() {
    _supply.dispose();
    _amount.dispose();
    _pin.dispose();
    super.dispose();
  }

  Future<void> _loadLinks() async {
    final service = widget.walletService;
    if (service == null) {
      if (!mounted) return;
      setState(() {
        _stage = _Stage.form;
        _linksError = 'Servicio de billetera no configurado.';
      });
      return;
    }
    try {
      final wallet = await service.getWallet();
      if (!mounted) return;
      final links =
          wallet.links.where((l) => l.isActive && l.isAccount).toList();
      setState(() {
        _links = links;
        _selectedLinkId = links.firstOrNull?.id;
        _stage = _Stage.form;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _links = [];
        _linksError = e.message;
        _stage = _Stage.form;
      });
    }
  }

  /// Monto en centimos desde texto `12.50` (parseo de presentacion; el
  /// backend valida y decide, la app no calcula comisiones ni totales).
  /// Rechaza mas de 2 decimales sin truncar en silencio (H2).
  int? _parseMinor(String text) {
    final clean = text.trim().replaceAll(',', '.');
    final parts = clean.split('.');
    if (parts.length > 2) return null;
    final soles = int.tryParse(parts[0]);
    if (soles == null || soles <= 0) return null;
    var cents = 0;
    if (parts.length == 2) {
      if (parts[1].length > 2) return null;
      final frac = parts[1].padRight(2, '0');
      cents = int.tryParse(frac) ?? -1;
      if (cents < 0) return null;
    }
    return soles * 100 + cents;
  }

  /// Detecta mas de 2 decimales para mensaje accionable (H2).
  bool _hasTooManyDecimals(String text) {
    final clean = text.trim().replaceAll(',', '.');
    final parts = clean.split('.');
    return parts.length == 2 && parts[1].length > 2;
  }

  Future<void> _pay() async {
    final service = widget.servicesService;
    final biller = widget.biller;
    final supply = _supply.text.trim();
    final minor = _parseMinor(_amount.text);
    if (service == null || biller == null) {
      setState(() {
        _failure = const ServiceFailure(
          message: 'Servicio de pagos no configurado o sin empresa elegida.',
          retry: false,
        );
        _stage = _Stage.failed;
      });
      return;
    }
    if (supply.isEmpty || supply.length > 80) {
      setState(() {
        _formError = 'Ingresa el número de suministro (1 a 80 caracteres).';
      });
      return;
    }
    if (minor == null) {
      if (_hasTooManyDecimals(_amount.text)) {
        setState(() {
          _formError = 'Usa máximo 2 decimales (céntimos).';
        });
      } else {
        setState(() {
          _formError = 'Ingresa un monto válido mayor a cero (ej. 25.50).';
        });
      }
      return;
    }
    final linkId = _selectedLinkId;
    if (linkId == null || linkId.isEmpty) {
      setState(() {
        _formError = 'Elige la fuente de fondos para pagar.';
      });
      return;
    }
    setState(() {
      _formError = null;
      _failure = null;
      _stage = _Stage.paying;
    });
    try {
      final result = await service.payService(
        billerId: biller.billerId,
        supplyRef: supply,
        amountMinor: minor,
        linkId: linkId,
        idempotencyKey: _payKey,
      );
      if (!mounted) return;
      if (result.isSettled) {
        setState(() {
          _result = result;
          _stage = _Stage.receipt;
        });
      } else if (result.isPendingAuth) {
        setState(() {
          _result = result;
          _stage = _Stage.pendingAuth;
        });
      } else if (result.isRejected) {
        setState(() {
          _result = result;
          _stage = _Stage.rejected;
        });
      } else {
        setState(() {
          _result = result;
          _failure = serviceFailureFor(
            code: 'UNKNOWN',
            fallback: 'El servidor respondió un estado no reconocido '
                '(${result.status}). Revisa tus movimientos antes de '
                'reintentarlo.',
          );
          _stage = _Stage.failed;
        });
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _failure = serviceFailureFor(
          code: e.code,
          fallback: e.message,
        );
        _stage = _Stage.failed;
      });
    }
  }

  Future<void> _authorizeBiometric() async {
    final controller = widget.authController;
    final result = _result;
    if (controller == null || result == null) {
      setState(() {
        _authMessage = 'Biometría no configurada en este dispositivo. '
            'Continúa con tu PIN de seguridad.';
        _showPin = widget.authService != null;
      });
      return;
    }
    final nonce = result.authNonce ?? '';
    if (nonce.isEmpty) {
      setState(() {
        _authMessage = 'El servidor no envió el reto de autorización. '
            'Vuelve a intentarlo.';
      });
      return;
    }
    setState(() {
      _authorizing = true;
      _authMessage = null;
    });
    try {
      final state = await controller.authorize(
        transactionId: result.transactionId,
        nonce: nonce,
        idempotencyKey: paymentAuthInitialKey(_payKey),
        reason: 'Autoriza el pago del servicio con tu biometría',
      );
      if (!mounted) return;
      switch (state.outcome) {
        case PaymentAuthOutcome.authorized:
          await _finalizeAfterAuth();
          return;
        case PaymentAuthOutcome.requiresPinFallback:
          setState(() {
            _authMessage = state.message.isEmpty
                ? 'Continúa con tu PIN de seguridad.'
                : state.message;
            _showPin = widget.authService != null;
          });
        case PaymentAuthOutcome.expired:
        case PaymentAuthOutcome.failed:
        case null:
          setState(() {
            _failure = serviceFailureFor(
              code: state.code ?? 'UNKNOWN',
              fallback: state.message.isEmpty
                  ? 'No pudimos autorizar el pago.'
                  : state.message,
            );
            _stage = _Stage.failed;
          });
      }
    } finally {
      if (mounted) setState(() => _authorizing = false);
    }
  }

  Future<void> _authorizePin() async {
    final service = widget.authService;
    final result = _result;
    final pin = _pin.text.trim();
    if (!RegExp(r'^\d{4,6}$').hasMatch(pin)) {
      setState(() {
        _pinError = 'Ingresa tu PIN de seguridad (4 a 6 dígitos).';
      });
      return;
    }
    if (service == null || result == null) return;
    setState(() {
      _authorizing = true;
      _pinError = null;
      _authMessage = null;
    });
    try {
      final authResult = await service.authorizeWithPin(
        transactionId: result.transactionId,
        pin: pin,
        idempotencyKey: paymentAuthInitialKey(_payKey),
      );
      if (!mounted) return;
      if (authResult.isRejected) {
        setState(() => _stage = _Stage.rejected);
      } else {
        await _finalizeAfterAuth();
        return;
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      if (e.code == 'NOT_AUTHORIZED') {
        setState(() => _pinError = 'PIN incorrecto. Inténtalo de nuevo.');
      } else {
        setState(() {
          _failure = serviceFailureFor(code: e.code, fallback: e.message);
          _stage = _Stage.failed;
        });
      }
    } finally {
      if (mounted) setState(() => _authorizing = false);
    }
  }

  /// Finaliza el diferido tras autorizar: replay con la MISMA
  /// `Idempotency-Key`; el comprobante solo sale con `SETTLED`.
  Future<void> _finalizeAfterAuth() async {
    final service = widget.servicesService;
    final biller = widget.biller;
    final linkId = _selectedLinkId;
    final minor = _parseMinor(_amount.text);
    if (service == null ||
        biller == null ||
        linkId == null ||
        linkId.isEmpty ||
        minor == null) {
      if (!mounted) return;
      setState(() {
        _authorizing = false;
        _failure = const ServiceFailure(
          message: 'No pudimos finalizar tu pago. Inténtalo de nuevo.',
          retry: true,
        );
        _stage = _Stage.failed;
      });
      return;
    }
    if (mounted) setState(() => _authMessage = 'Finalizando tu pago…');
    try {
      final finalResult = await service.payService(
        billerId: biller.billerId,
        supplyRef: _supply.text.trim(),
        amountMinor: minor,
        linkId: linkId,
        idempotencyKey: _payKey,
      );
      if (!mounted) return;
      if (finalResult.isSettled) {
        setState(() {
          _result = finalResult;
          _authMessage = null;
          _stage = _Stage.receipt;
        });
      } else if (finalResult.isRejected) {
        setState(() {
          _result = finalResult;
          _authMessage = null;
          _stage = _Stage.rejected;
        });
      } else {
        setState(() {
          _result = finalResult;
          _failure = serviceFailureFor(
            code: 'UNKNOWN',
            fallback: 'El servidor respondió un estado no reconocido '
                '(${finalResult.status}). Revisa tus movimientos antes de '
                'reintentarlo.',
          );
          _stage = _Stage.failed;
        });
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _failure = serviceFailureFor(code: e.code, fallback: e.message);
        _stage = _Stage.failed;
      });
    } finally {
      if (mounted) setState(() => _authorizing = false);
    }
  }

  void _retry() => _pay();

  void _done() {
    final onDone = widget.onDone;
    if (onDone != null) {
      onDone();
      return;
    }
    context.go('/wallet/services');
  }

  @override
  Widget build(BuildContext context) {
    final biller = widget.biller;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Pagar servicio')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        child: biller == null ? _buildNoBiller() : _buildStage(biller),
      ),
    );
  }

  Widget _buildNoBiller() {
    return EmptyView(
      key: const Key('service-pay-empty-biller'),
      message: 'Elige primero una empresa en el catálogo de servicios.',
      actionLabel: 'Ver catálogo',
      onAction: () => context.go('/wallet/services'),
    );
  }

  Widget _buildStage(Biller biller) {
    switch (_stage) {
      case _Stage.loading:
        return const Center(
          child: Padding(
            padding: EdgeInsets.symmetric(vertical: 48),
            child: CircularProgressIndicator(
              key: Key('service-pay-loading'),
            ),
          ),
        );
      case _Stage.form:
        return _buildForm(biller);
      case _Stage.paying:
        return const Center(
          child: Padding(
            padding: EdgeInsets.symmetric(vertical: 48),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                CircularProgressIndicator(key: Key('service-pay-paying')),
                SizedBox(height: 12),
                Text('Procesando tu pago…'),
              ],
            ),
          ),
        );
      case _Stage.pendingAuth:
        return _buildPendingAuth();
      case _Stage.receipt:
        final result = _result;
        if (result == null) return _buildForm(biller);
        return ServiceReceiptCard(result: result, onDone: _done);
      case _Stage.rejected:
        final result = _result;
        return ServiceErrorCard(
          failure: serviceFailureFor(
            code: 'UNKNOWN',
            failureCode: result?.failureCode,
            fallback: 'El pago fue rechazado. No se descontó nada.',
          ),
          onBack: () => setState(() => _stage = _Stage.form),
        );
      case _Stage.failed:
        return ServiceErrorCard(
          failure: _failure ??
              const ServiceFailure(
                message: 'Ocurrió un error inesperado. Inténtalo de nuevo.',
                retry: true,
              ),
          onRetry: (_failure?.retry ?? true) ? _retry : null,
          onBack: () => setState(() => _stage = _Stage.form),
        );
    }
  }

  Widget _buildForm(Biller biller) {
    if (_linksError != null && _links.isEmpty) {
      return ServiceErrorCard(
        failure: ServiceFailure(message: _linksError!, retry: true),
        onRetry: () {
          setState(() => _stage = _Stage.loading);
          _loadLinks();
        },
      );
    }
    if (_links.isEmpty) {
      return EmptyView(
        key: const Key('service-pay-empty-links'),
        message: 'Aún no tienes una fuente de fondos para pagar.\n'
            'Vincula una cuenta del banco para pagar servicios.',
        actionLabel: 'Vincular fuente',
        onAction: () => context.push('/wallet/link'),
      );
    }
    return AppCard(
      key: const Key('service-pay-form'),
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(biller.name, style: AppTypography.titleMd),
              ),
              AppStatusChip.info(label: biller.category),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            'Los montos y el comprobante los define el servidor.',
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            key: const Key('service-pay-supply-field'),
            controller: _supply,
            decoration: InputDecoration(
              labelText: 'Número de suministro',
              hintText: 'Ej. 12345678',
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
          ),
          const SizedBox(height: 8),
          TextField(
            key: const Key('service-pay-amount-field'),
            controller: _amount,
            keyboardType:
                const TextInputType.numberWithOptions(decimal: true),
            decoration: InputDecoration(
              labelText: 'Monto (S/)',
              hintText: 'Ej. 25.50',
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
          ),
          const SizedBox(height: 8),
          DropdownButtonFormField<String>(
            key: const Key('service-pay-link-field'),
            initialValue: _selectedLinkId,
            decoration: InputDecoration(
              labelText: 'Fuente de fondos',
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
            items: [
              for (final link in _links)
                DropdownMenuItem(value: link.id, child: Text(link.maskedLabel)),
            ],
            onChanged: (v) => setState(() => _selectedLinkId = v),
          ),
          if (_formError != null) ...[
            const SizedBox(height: 8),
            Text(
              _formError!,
              key: const Key('service-pay-form-error'),
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.onErrorContainer,
              ),
            ),
          ],
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            height: 52,
            child: FilledButton.icon(
              key: const Key('service-pay-confirm'),
              onPressed: _pay,
              icon: const Icon(Icons.payments_outlined, size: 20),
              label: const Text(
                'Confirmar pago',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildPendingAuth() {
    final result = _result;
    if (result == null) return const SizedBox.shrink();
    return Column(
      children: [
        AppCard(
          key: const Key('service-pay-pending-card'),
          padding: const EdgeInsets.all(20),
          child: Column(
            children: [
              const Row(
                children: [
                  Expanded(
                    child: Text(
                      'Confirma el pago',
                      style: TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  AppStatusChip.warning(label: 'Por autorizar'),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                formatServiceMinor(result.amountMinor, result.currency),
                style: AppTypography.displayLg.copyWith(
                  color: AppColors.primary,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'Este monto supera el límite base: confirma con tu '
                'biometría o tu PIN para autorizarlo.',
                textAlign: TextAlign.center,
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 13,
                  color: AppColors.secondaryText,
                ),
              ),
              if (_authMessage != null) ...[
                const SizedBox(height: 8),
                Text(
                  _authMessage!,
                  key: const Key('service-pay-auth-message'),
                  textAlign: TextAlign.center,
                  style: AppTypography.labelMd.copyWith(fontSize: 13),
                ),
              ],
              const SizedBox(height: 12),
              SizedBox(
                width: double.infinity,
                height: 52,
                child: FilledButton.icon(
                  key: const Key('service-pay-authorize-biometric'),
                  onPressed: _authorizing ? null : _authorizeBiometric,
                  icon: _authorizing
                      ? const SizedBox.square(
                          dimension: 20,
                          child: CircularProgressIndicator(strokeWidth: 2.5),
                        )
                      : const Icon(Icons.fingerprint, size: 20),
                  label: const Text(
                    'Autorizar con biometría',
                    style: TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ),
              if (widget.authService != null) ...[
                TextButton(
                  key: const Key('service-pay-authorize-pin-toggle'),
                  onPressed: () => setState(() => _showPin = !_showPin),
                  child: Text(_showPin ? 'Ocultar PIN' : 'Usar PIN'),
                ),
                if (_showPin) ...[
                  TextField(
                    key: const Key('service-pay-pin-field'),
                    controller: _pin,
                    obscureText: true,
                    keyboardType: TextInputType.number,
                    maxLength: 6,
                    decoration: InputDecoration(
                      labelText: 'PIN de seguridad',
                      hintText: '4 a 6 dígitos',
                      filled: true,
                      fillColor: AppColors.surfaceContainerLowest,
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                      errorText: _pinError,
                    ),
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    width: double.infinity,
                    height: 48,
                    child: OutlinedButton(
                      key: const Key('service-pay-authorize-pin'),
                      onPressed: _authorizing ? null : _authorizePin,
                      child: const Text('Autorizar con PIN'),
                    ),
                  ),
                ],
              ],
            ],
          ),
        ),
      ],
    );
  }
}
