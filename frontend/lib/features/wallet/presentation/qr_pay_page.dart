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
import '../data/qr_payment_service.dart';
import '../data/wallet_service.dart';
import '../models/merchant_qr.dart' show parseSolesToMinor;
import '../models/qr_payment.dart';
import '../models/wallet.dart';

/// Resumen, confirmacion y resultado del pago QR (HU15, E4-T13 + E4-T14;
/// sin pagina en `pantallas.fig` — fila pendiente `billetera-qr-pago.png`,
/// G76; UI desde tokens `docs/20`).
///
/// Flujo (cliente delgado estricto, docs/19 §4):
/// 1. **Resumen previo** (`POST /payments/qr/preview` al detectar el
///    `qr_content`, fix MEDIO-3, `docs/05` §6.5): `comercio`/`monto` tal
///    cual del servidor + fuente de fondos (`GET /wallet`, solo vinculos
///    `ACCOUNT` activos, enmascarados) + **Confirmar pago en un toque**.
///    `422` (vencido/corrupto/manipulado/ajeno) y `409` (ya cobrado) se
///    muestran en español sin cobrar. El `qr_content` nunca se muestra
///    (E4-T14).
/// 2. **Confirmar** (`POST /payments/qr` + `Idempotency-Key` del intento;
///    fix N-1: el Reintentar conserva la misma clave solo en fallos de
///    red/`UNKNOWN`; tras un `REJECTED` definitivo rota a clave nueva):
///    - `SETTLED` -> recibo con `comercio`/`monto` del servidor.
///    - `PENDING_AUTHORIZATION` -> resumen del servidor
///      (`comercio`/`monto` + reto) y **biometria local o PIN**
///      (`POST /transactions/{id}/authorize`, patron F-T03) antes de
///      mover el dinero; tras autorizar se **reintenta `POST
///      /payments/qr` con la MISMA `Idempotency-Key`** para finalizar
///      el diferido (fix ALTO-1) y el recibo solo se muestra cuando el
///      reintento confirma `SETTLED`.
///    - `REJECTED` (`failure_code`) o error HTTP -> mensaje accionable
///      en español + recuperacion (E4-T14), sin descontar fondos.
///
/// Estados (docs/20 §7): contenido / carga / vacio / error con
/// Reintentar; mensajes en español; sin logica offline.
class QrPayPage extends StatefulWidget {
  const QrPayPage({
    super.key,
    required this.qrContent,
    this.paymentService,
    this.walletService,
    this.authController,
    this.authService,
    this.initialLinkId,
    this.onRescan,
    this.onDone,
  });

  /// Sobre `BO-QR1` tal cual escaneado (efimero en memoria; nunca se
  /// muestra ni se loguea).
  final String qrContent;

  final QrPaymentServiceBase? paymentService;
  final WalletServiceBase? walletService;

  /// Controlador del reto biometrico (prod: `paymentAuthorizationDeps`
  /// del orquestador; tests: controlador con fakes). `null` = biometria
  /// no configurada (se ofrece PIN si hay [authService]).
  final PaymentAuthorizationController? authController;

  /// Servicio de autorizacion para el fallback a PIN (`authorizeWithPin`,
  /// E2-T31). `null` = PIN no disponible.
  final PaymentAuthorizationServiceBase? authService;

  final String? initialLinkId;
  final VoidCallback? onRescan;
  final VoidCallback? onDone;

  @override
  State<QrPayPage> createState() => _QrPayPageState();
}

enum _Stage {
  loadingLinks,
  summary,
  paying,
  pendingAuth,
  receipt,
  rejected,
  failed,
}

class _QrPayPageState extends State<QrPayPage> {
  /// Clave del intento vigente (fix N-1 HU15):
  /// - Se reutiliza la MISMA clave solo cuando el resultado es
  ///   incierto (fallo de red/`UNKNOWN` con `QrPayRecovery.retry`) y
  ///   para la finalización del diferido tras autorizar (replay
  ///   intencional con la clave original, fix ALTO-1).
  /// - Tras un `REJECTED` definitivo/de negocio (resultado conocido,
  ///   sin mover dinero: `INSUFFICIENT_FUNDS`,
  ///   `DAILY_LIMIT_EXCEEDED`, `BIOMETRIC_PREVENTIVE_BLOCK`, etc.),
  ///   el Reintentar genera una NUEVA clave (nueva intención): el
  ///   backend cachea el `REJECTED` bajo la clave original y con la
  ///   misma clave nunca liquidaría aunque ya haya fondos.
  String _payKey = const Uuid().v4();

  /// Último `error.code` del `POST /payments/qr` que llevó a
  /// `_Stage.failed` (mayúsculas; `null` sin error previo). Permite
  /// distinguir en el Reintentar un fallo de negocio definitivo (nueva
  /// clave) de un fallo incierto de red/`UNKNOWN` (misma clave).
  String? _lastPayErrorCode;

  /// Códigos de negocio definitivos (resultado conocido, sin mover
  /// dinero): reintentar exige nueva `Idempotency-Key`. Red/`UNKNOWN`
  /// y el resto conservan la clave (resultado incierto, anti-duplicado).
  static const _definitiveBusinessCodes = <String>{
    'INSUFFICIENT_FUNDS',
    'NOT_SUFFICIENT_FUNDS',
    'DAILY_LIMIT_EXCEEDED',
    'BIOMETRIC_PREVENTIVE_BLOCK',
  };

  /// `true` si el próximo Reintentar debe usar una clave nueva: el
  /// intento vigente ya tiene un resultado definitivo conocido
  /// (`REJECTED` en `_result`, con o sin `failure_code`) o el fallo
  /// previo fue un código de negocio definitivo.
  bool _needsNewPayKey() {
    if (_result?.isRejected ?? false) return true;
    final code = _lastPayErrorCode?.toUpperCase();
    return code != null && _definitiveBusinessCodes.contains(code);
  }

  void _rotatePayKey() {
    _payKey = const Uuid().v4();
  }

  _Stage _stage = _Stage.loadingLinks;
  List<WalletLink> _links = [];
  String? _selectedLinkId;
  String? _linksError;

  /// Vista previa del cargo (fix MEDIO-3): `comercio`/`monto` del
  /// servidor antes de confirmar (solo lectura, sin mover dinero).
  QrChargePreview? _preview;
  bool _previewLoading = true;
  QrPayFailure? _previewFailure;

  QrPaymentResult? _result;
  QrPayFailure? _failure;
  String? _authMessage;
  bool _showPin = false;
  bool _authorizing = false;
  final TextEditingController _pinController = TextEditingController();
  String? _pinError;

  /// Monto digitado por el pagador (soles) cuando el QR no trae monto
  /// (personal, E4-T21): solo captura y muestra; el backend valida y
  /// decide el cobro (cliente delgado, docs/19 §4).
  final TextEditingController _amountController = TextEditingController();
  String? _amountError;

  /// Monto en centimos confirmado para el intento vigente (se reusa en
  /// la finalizacion del diferido con la misma `Idempotency-Key`: el
  /// backend exige el cuerpo exacto en el replay).
  int? _confirmedAmountMinor;

  @override
  void initState() {
    super.initState();
    if (widget.qrContent.isEmpty) {
      _stage = _Stage.failed;
      _failure = const QrPayFailure(
        message: 'Sin QR para pagar. Escanea el QR del comercio para '
            'continuar.',
        recovery: QrPayRecovery.rescan,
      );
      return;
    }
    _loadLinks();
    _loadPreview();
  }

  @override
  void dispose() {
    _pinController.dispose();
    _amountController.dispose();
    super.dispose();
  }

  Future<void> _loadLinks() async {
    final service = widget.walletService;
    if (service == null) {
      if (!mounted) return;
      setState(() {
        _stage = _Stage.summary;
        _links = [];
        _linksError = 'Servicio de billetera no configurado.';
      });
      return;
    }
    try {
      final wallet = await service.getWallet();
      if (!mounted) return;
      final links = wallet.links
          .where((l) => l.isActive && l.isAccount)
          .toList();
      setState(() {
        _links = links;
        _selectedLinkId = widget.initialLinkId ?? links.firstOrNull?.id;
        _stage = _Stage.summary;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _links = [];
        _linksError = e.message;
        _stage = _Stage.summary;
      });
    }
  }

  /// Carga la vista previa del cargo (fix MEDIO-3, `POST
  /// `/payments/qr/preview`): `comercio`/`monto` del servidor antes de
  /// confirmar, sin mover dinero. `422`/`409` se traducen a mensaje en
  /// español y bloquean el Confirmar (sin cobrar).
  Future<void> _loadPreview() async {
    final service = widget.paymentService;
    if (service == null) {
      if (!mounted) return;
      setState(() {
        _previewLoading = false;
        _previewFailure = const QrPayFailure(
          message: 'Servicio de pagos no configurado.',
          recovery: QrPayRecovery.retry,
        );
      });
      return;
    }
    setState(() {
      _previewLoading = true;
      _previewFailure = null;
    });
    try {
      final preview = await service.previewQr(
        qrContent: widget.qrContent,
      );
      if (!mounted) return;
      setState(() {
        _preview = preview;
        _previewLoading = false;
        _previewFailure = null;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _preview = null;
        _previewLoading = false;
        _previewFailure = qrPayFailureFor(
          code: e.code,
          serverHint: e.serverMessage,
          fallback: e.message,
        );
      });
    }
  }

  Future<void> _pay() async {
    final service = widget.paymentService;
    final linkId = _selectedLinkId;
    if (service == null) {
      setState(() {
        _failure = const QrPayFailure(
          message: 'Servicio de pagos no configurado.',
          recovery: QrPayRecovery.retry,
        );
        _stage = _Stage.failed;
      });
      return;
    }
    if (linkId == null || linkId.isEmpty) {
      setState(() {
        _failure = const QrPayFailure(
          message: 'Elige la fuente de fondos para pagar.',
          recovery: QrPayRecovery.retry,
        );
        _stage = _Stage.failed;
      });
      return;
    }
    // QR personal sin monto (E4-T21): el pagador digita el monto en
    // soles; se codifica a centimos exactos sin `double` (solo
    // transporte; el backend valida `> 0` y decide el cobro).
    int? amountMinor;
    if (_preview?.isPersonal ?? false) {
      amountMinor = parseSolesToMinor(_amountController.text);
      if (amountMinor == null || amountMinor <= 0) {
        setState(() {
          _amountError = 'Ingresa el monto a pagar en soles (mayor a S/ 0.00).';
        });
        return;
      }
      _confirmedAmountMinor = amountMinor;
    } else {
      amountMinor = _confirmedAmountMinor;
    }
    setState(() {
      _stage = _Stage.paying;
      _failure = null;
    });
    try {
      final result = await service.payQr(
        qrContent: widget.qrContent,
        linkId: linkId,
        amountMinor: amountMinor,
        idempotencyKey: _payKey,
      );
      if (!mounted) return;
      if (result.isSettled) {
        setState(() {
          _result = result;
          _lastPayErrorCode = null;
          _stage = _Stage.receipt;
        });
      } else if (result.isPendingAuth) {
        setState(() {
          _result = result;
          _lastPayErrorCode = null;
          _stage = _Stage.pendingAuth;
        });
      } else if (result.isRejected) {
        setState(() {
          _result = result;
          _lastPayErrorCode = null;
          _stage = _Stage.rejected;
        });
      } else {
        setState(() {
          _result = result;
          _lastPayErrorCode = 'UNKNOWN';
          _failure = qrPayFailureFor(
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
        // Fix N-1: se guarda el `code` para que `_retry` decida si el
        // próximo intento conserva `_payKey` (red/`UNKNOWN`, resultado
        // incierto) o rota a una clave nueva (negocio definitivo).
        _lastPayErrorCode = e.code.toUpperCase();
        _failure = qrPayFailureFor(
          code: e.code,
          serverHint: e.serverMessage,
          fallback: e.message,
        );
        _stage = _Stage.failed;
      });
    }
  }

  /// Autoriza el reto con biometria local (solo factor del reto del
  /// motor; el umbral lo decidio el servidor con `PENDING_AUTHORIZATION`).
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
        // Clave derivada del intento (patron E2-T21: el `authorize` nunca
        // reusa la clave del pago tal cual).
        idempotencyKey: paymentAuthInitialKey(_payKey),
        reason: 'Autoriza el pago QR con tu biometría',
      );
      if (!mounted) return;
      switch (state.outcome) {
        case PaymentAuthOutcome.authorized:
          final authResult = state.result;
          if (authResult != null && authResult.isRejected) {
            setState(() {
              _authorizing = false;
              _stage = _Stage.rejected;
            });
            return;
          }
          // Fix ALTO-1: tras autorizar, el backend finaliza el diferido
          // cuando se reintenta `POST /payments/qr` con la MISMA
          // `Idempotency-Key`. El recibo solo se muestra si ese reintento
          // confirma `SETTLED` (no basta el `authorize`).
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
            _failure = qrPayFailureFor(
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

  /// Autoriza el reto con el PIN del titular (E2-T31, fallback).
  Future<void> _authorizePin() async {
    final service = widget.authService;
    final result = _result;
    final pin = _pinController.text.trim();
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
        // Fix ALTO-1: igual que la biometria, el PIN solo autoriza el
        // reto; la finalizacion la dispara el replay con la misma clave.
        await _finalizeAfterAuth();
        return;
      }
    } on ApiException catch (e) {
      if (!mounted) return;
      if (e.code == 'NOT_AUTHORIZED') {
        setState(() {
          _pinError = 'PIN incorrecto. Inténtalo de nuevo.';
        });
      } else {
        setState(() {
          _failure = qrPayFailureFor(
            code: e.code,
            fallback: e.message,
          );
          _stage = _Stage.failed;
        });
      }
    } finally {
      if (mounted) setState(() => _authorizing = false);
    }
  }

  /// Finaliza el pago diferido tras autorizar (fix ALTO-1): reintenta
  /// `POST /payments/qr` con la MISMA `Idempotency-Key` (`_payKey`).
  /// El backend marca el cargo `PAID` y emite el abono; el recibo solo
  /// se muestra cuando el reintento confirma `SETTLED`.
  Future<void> _finalizeAfterAuth() async {
    final service = widget.paymentService;
    final linkId = _selectedLinkId;
    if (service == null || linkId == null || linkId.isEmpty) {
      if (!mounted) return;
      setState(() {
        _authorizing = false;
        _failure = const QrPayFailure(
          message: 'No pudimos finalizar tu pago. Inténtalo de nuevo.',
          recovery: QrPayRecovery.retry,
        );
        _stage = _Stage.failed;
      });
      return;
    }
    if (mounted) {
      setState(() {
        _authMessage = 'Finalizando tu pago…';
      });
    }
    try {
      final finalResult = await service.payQr(
        qrContent: widget.qrContent,
        linkId: linkId,
        amountMinor: _confirmedAmountMinor,
        idempotencyKey: _payKey,
      );
      if (!mounted) return;
      if (finalResult.isSettled) {
        setState(() {
          _result = finalResult;
          _lastPayErrorCode = null;
          _authMessage = null;
          _stage = _Stage.receipt;
        });
      } else if (finalResult.isRejected) {
        setState(() {
          _result = finalResult;
          _lastPayErrorCode = null;
          _authMessage = null;
          _stage = _Stage.rejected;
        });
      } else if (finalResult.isPendingAuth) {
        setState(() {
          _result = finalResult;
          _lastPayErrorCode = null;
          _authMessage = 'Tu pago sigue pendiente de autorización. '
              'Vuelve a intentarlo.';
        });
      } else {
        setState(() {
          _result = finalResult;
          _lastPayErrorCode = 'UNKNOWN';
          _failure = qrPayFailureFor(
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
        _lastPayErrorCode = e.code.toUpperCase();
        _failure = qrPayFailureFor(
          code: e.code,
          serverHint: e.serverMessage,
          fallback: e.message,
        );
        _stage = _Stage.failed;
      });
    } finally {
      if (mounted) setState(() => _authorizing = false);
    }
  }

  void _retry() {
    // Fix N-1: tras un `REJECTED` definitivo (resultado conocido, sin
    // mover dinero) se rota a una NUEVA `Idempotency-Key` (nueva
    // intención); el backend cachea el `REJECTED` bajo la clave original
    // y con la misma clave nunca liquidaría. Solo los fallos de
    // red/`UNKNOWN` (resultado incierto) conservan `_payKey`.
    if (_needsNewPayKey()) _rotatePayKey();
    setState(() {
      _failure = null;
      _authMessage = null;
      _lastPayErrorCode = null;
    });
    _pay();
  }

  void _rescan() {
    final onRescan = widget.onRescan;
    if (onRescan != null) {
      onRescan();
      return;
    }
    if (Navigator.of(context).canPop()) {
      Navigator.of(context).pop();
    } else {
      context.push('/wallet/pay-qr');
    }
  }

  void _done() {
    final onDone = widget.onDone;
    if (onDone != null) {
      onDone();
      return;
    }
    context.go('/wallet');
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Pagar con QR')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        child: _buildStage(),
      ),
    );
  }

  Widget _buildStage() {
    switch (_stage) {
      case _Stage.loadingLinks:
        return const Center(
          child: Padding(
            padding: EdgeInsets.symmetric(vertical: 48),
            child: CircularProgressIndicator(
              key: Key('qr-pay-loading-links'),
            ),
          ),
        );
      case _Stage.summary:
        return _buildSummary();
      case _Stage.paying:
        return const Center(
          child: Padding(
            padding: EdgeInsets.symmetric(vertical: 48),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                CircularProgressIndicator(key: Key('qr-pay-paying')),
                SizedBox(height: 12),
                Text('Procesando tu pago…'),
              ],
            ),
          ),
        );
      case _Stage.pendingAuth:
        return _buildPendingAuth();
      case _Stage.receipt:
        return _buildReceipt();
      case _Stage.rejected:
        return _buildRejected();
      case _Stage.failed:
        return _buildFailure();
    }
  }

  /// Resumen previo al primer toque (E4-T13 + fix MEDIO-3): vista
  /// previa del servidor (`POST /payments/qr/preview` con `comercio`/
  /// `monto`) + fuente de fondos. El `qr_content` nunca se muestra
  /// (E4-T14). `422`/`409` del preview bloquean el Confirmar sin cobrar.
  Widget _buildSummary() {
    if (_linksError != null && _links.isEmpty) {
      return Column(
        children: [
          _QrPayErrorCard(
            failure: qrPayFailureFor(
              code: 'UNKNOWN',
              fallback: _linksError!,
            ),
            onRetry: () {
              setState(() => _stage = _Stage.loadingLinks);
              _loadLinks();
            },
            onRescan: _rescan,
          ),
        ],
      );
    }
    if (_links.isEmpty) {
      return EmptyView(
        key: const Key('qr-pay-empty-links'),
        message: 'Aún no tienes una fuente de fondos para pagar.\n'
            'Vincula una cuenta del banco para pagar con QR.',
        actionLabel: 'Vincular fuente',
        onAction: () => context.push('/wallet/link'),
      );
    }
    // Preview invalido (422 vencido/corrupto/manipulado/ajeno, 409 ya
    // cobrado): se muestra en español y no se cobra (sin Confirmar).
    final previewFailure = _previewFailure;
    if (!_previewLoading && previewFailure != null) {
      return Column(
        children: [
          _QrPayErrorCard(
            failure: previewFailure,
            onRetry: previewFailure.recovery == QrPayRecovery.retry
                ? () => _loadPreview()
                : null,
            onRescan:
                previewFailure.recovery == QrPayRecovery.rescan ? _rescan : null,
          ),
        ],
      );
    }
    return AppCard(
      key: const Key('qr-pay-summary'),
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 44,
                height: 44,
                alignment: Alignment.center,
                decoration: const BoxDecoration(
                  shape: BoxShape.circle,
                  color: AppColors.surfaceContainerHigh,
                ),
                child: const Icon(
                  Icons.qr_code_2,
                  color: AppColors.primary,
                  size: 24,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(
                  'QR detectado',
                  style: AppTypography.titleMd.copyWith(fontSize: 17),
                ),
              ),
              const AppStatusChip.info(label: 'Por confirmar'),
            ],
          ),
          const SizedBox(height: 8),
          if (_previewLoading) ...[
            const Center(
              child: Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: CircularProgressIndicator(
                  key: Key('qr-pay-preview-loading'),
                ),
              ),
            ),
            Text(
              'Confirmando comercio y monto con el servidor…',
              style: AppTypography.bodyMd.copyWith(
                fontSize: 13,
                color: AppColors.secondaryText,
              ),
            ),
          ] else if (_preview != null) ...[
            Text(
              _preview!.merchantName.isEmpty
                  ? 'QR personal confirmado por el servidor'
                  : _preview!.merchantName,
              key: const Key('qr-pay-preview-merchant'),
              style: AppTypography.titleMd.copyWith(fontSize: 16),
            ),
            const SizedBox(height: 4),
            if (_preview!.isPersonal) ...[
              Text(
                'Este QR no trae monto: digita cuánto quieres pagar. '
                'El servidor valida y confirma el cobro.',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 13,
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: 8),
              TextField(
                key: const Key('qr-pay-amount-field'),
                controller: _amountController,
                keyboardType: const TextInputType.numberWithOptions(
                  decimal: true,
                ),
                decoration: InputDecoration(
                  labelText: 'Monto a pagar (soles)',
                  hintText: 'Ej. 25.50',
                  filled: true,
                  fillColor: AppColors.surfaceContainerLowest,
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                  errorText: _amountError,
                ),
                onChanged: (_) {
                  if (_amountError != null) {
                    setState(() => _amountError = null);
                  }
                },
              ),
            ] else ...[
              Text(
                formatQrMinor(_preview!.amountMinor, _preview!.currency),
                key: const Key('qr-pay-preview-amount'),
                style: AppTypography.displayLg.copyWith(
                  color: AppColors.primary,
                ),
              ),
            ],
            const SizedBox(height: 4),
            Text(
              'Valores del servidor antes de confirmar. Revisa la fuente '
              'y confirma en un toque.',
              style: AppTypography.bodyMd.copyWith(
                fontSize: 13,
                color: AppColors.secondaryText,
              ),
            ),
          ] else ...[
            Text(
              'El comercio y el monto los confirma el servidor al procesar '
              'el pago. Revisa la fuente y confirma en un toque.',
              style: AppTypography.bodyMd.copyWith(
                fontSize: 13,
                color: AppColors.secondaryText,
              ),
            ),
          ],
          const SizedBox(height: 16),
          DropdownButtonFormField<String>(
            key: const Key('qr-pay-link-field'),
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
                DropdownMenuItem(
                  value: link.id,
                  child: Text(link.maskedLabel),
                ),
            ],
            onChanged: (value) => setState(() => _selectedLinkId = value),
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            height: 52,
            child: FilledButton.icon(
              key: const Key('qr-pay-confirm'),
              onPressed: _previewLoading ? null : _pay,
              icon: const Icon(Icons.payments_outlined, size: 20),
              label: const Text(
                'Confirmar pago',
                style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
              ),
            ),
          ),
          TextButton.icon(
            key: const Key('qr-pay-rescan-summary'),
            onPressed: _rescan,
            icon: const Icon(Icons.qr_code_scanner, size: 18),
            label: const Text('Escanear otro QR'),
          ),
        ],
      ),
    );
  }

  /// Resumen del servidor sobre el limite (E4-T13): `comercio`/`monto`
  /// tal cual del backend + biometria o PIN para autorizar el reto.
  Widget _buildPendingAuth() {
    final result = _result;
    if (result == null) return _buildFailure();
    return Column(
      children: [
        AppCard(
          key: const Key('qr-pay-pending-card'),
          padding: const EdgeInsets.all(20),
          child: Column(
            children: [
              Row(
                children: [
                  const Expanded(
                    child: Text(
                      'Confirma el pago',
                      style: TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  const AppStatusChip.warning(label: 'Por autorizar'),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                result.displayName,
                key: const Key('qr-pay-merchant'),
                style: AppTypography.titleMd.copyWith(fontSize: 16),
              ),
              const SizedBox(height: 4),
              Text(
                formatQrMinor(result.amountMinor, result.currency),
                key: const Key('qr-pay-amount'),
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
                  key: const Key('qr-pay-auth-message'),
                  textAlign: TextAlign.center,
                  style: AppTypography.labelMd.copyWith(
                    fontSize: 13,
                    color: AppColors.onErrorContainer,
                  ),
                ),
              ],
              const SizedBox(height: 12),
              SizedBox(
                width: double.infinity,
                height: 52,
                child: FilledButton.icon(
                  key: const Key('qr-pay-authorize-biometric'),
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
                  key: const Key('qr-pay-authorize-pin-toggle'),
                  onPressed: () => setState(() => _showPin = !_showPin),
                  child: Text(
                    _showPin ? 'Ocultar PIN' : 'Usar PIN de seguridad',
                  ),
                ),
                if (_showPin) ...[
                  TextField(
                    key: const Key('qr-pay-pin-field'),
                    controller: _pinController,
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
                    onChanged: (_) {
                      if (_pinError != null) {
                        setState(() => _pinError = null);
                      }
                    },
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    width: double.infinity,
                    height: 48,
                    child: OutlinedButton(
                      key: const Key('qr-pay-authorize-pin'),
                      onPressed: _authorizing ? null : _authorizePin,
                      child: const Text('Autorizar con PIN'),
                    ),
                  ),
                ],
              ],
            ],
          ),
        ),
        TextButton.icon(
          key: const Key('qr-pay-rescan-pending'),
          onPressed: _rescan,
          icon: const Icon(Icons.qr_code_scanner, size: 18),
          label: const Text('Cancelar y escanear otro QR'),
        ),
      ],
    );
  }

  /// Comprobante `SETTLED` (E4-T13): `comercio`/`monto` del servidor, sin
  /// recalculo del cliente.
  Widget _buildReceipt() {
    final result = _result;
    if (result == null) return _buildFailure();
    return AppCard(
      key: const Key('qr-pay-receipt'),
      padding: const EdgeInsets.all(20),
      child: Column(
        children: [
          const AppStatusChip.success(
            key: Key('qr-pay-result-success'),
            label: 'Pagado',
          ),
          const SizedBox(height: 12),
          Text(
            result.isPersonal
                ? 'Pago acreditado a su titular'
                : (result.merchantName.isEmpty
                    ? 'Pago acreditado al comercio'
                    : result.merchantName),
            key: const Key('qr-pay-merchant'),
            textAlign: TextAlign.center,
            style: AppTypography.titleMd.copyWith(fontSize: 17),
          ),
          const SizedBox(height: 4),
          Text(
            formatQrMinor(result.amountMinor, result.currency),
            key: const Key('qr-pay-amount'),
            style: AppTypography.displayLg.copyWith(
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            'Operación ${result.transactionId.length > 8 ? result.transactionId.substring(0, 8).toUpperCase() : result.transactionId.toUpperCase()}… · '
            '${result.currency}',
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            height: 52,
            child: FilledButton(
              key: const Key('qr-pay-done'),
              onPressed: _done,
              child: const Text('Volver a billetera'),
            ),
          ),
        ],
      ),
    );
  }

  /// Rechazo sin descontar fondos (E4-T14): mensaje por `failure_code` +
  /// recuperacion, nunca marcado como exitoso.
  Widget _buildRejected() {
    final result = _result;
    final failure = qrPayFailureFor(
      code: 'UNKNOWN',
      failureCode: result?.failureCode,
      fallback: 'El pago fue rechazado. No se descontó nada de tu cuenta.',
    );
    return Column(
      children: [
        _QrPayErrorCard(
          failure: failure,
          merchant: result?.merchantName,
          amountMinor: result?.amountMinor,
          currency: result?.currency,
          onRetry: failure.recovery == QrPayRecovery.retry ? _retry : null,
          onRescan: _rescan,
        ),
      ],
    );
  }

  Widget _buildFailure() {
    final failure = _failure ??
        const QrPayFailure(
          message: 'Ocurrió un error inesperado. Inténtalo de nuevo.',
          recovery: QrPayRecovery.retry,
        );
    return Column(
      children: [
        _QrPayErrorCard(
          failure: failure,
          onRetry: failure.recovery == QrPayRecovery.retry ? _retry : null,
          onRescan:
              failure.recovery == QrPayRecovery.rescan ? _rescan : null,
        ),
      ],
    );
  }
}

/// Tarjeta de error de pago QR (E4-T14): alerta visual en
/// `error-container` + badge segun el tipo + mensaje accionable en
/// español + recuperacion (Reintentar / Re-escanear).
///
/// Nunca muestra el `qr_content` ni datos sensibles: solo el mensaje
/// local derivado del `code` del backend.
class _QrPayErrorCard extends StatelessWidget {
  const _QrPayErrorCard({
    required this.failure,
    this.merchant,
    this.amountMinor,
    this.currency,
    this.onRetry,
    this.onRescan,
  });

  final QrPayFailure failure;
  final String? merchant;
  final int? amountMinor;
  final String? currency;
  final VoidCallback? onRetry;
  final VoidCallback? onRescan;

  @override
  Widget build(BuildContext context) {
    return Container(
      key: const Key('qr-pay-error'),
      width: double.infinity,
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: AppColors.errorContainer,
        borderRadius: BorderRadius.circular(16),
      ),
      child: Column(
        children: [
          const Icon(
            Icons.qr_code_2,
            size: 48,
            color: AppColors.onErrorContainer,
          ),
          const SizedBox(height: 8),
          if (failure.invalidQr)
            const AppStatusChip.error(
              key: Key('qr-pay-invalid-badge'),
              label: 'QR inválido',
            )
          else
            const AppStatusChip.error(label: 'Pago no realizado'),
          const SizedBox(height: 8),
          if ((merchant ?? '').isNotEmpty) ...[
            Text(
              merchant!,
              style: AppTypography.titleMd.copyWith(
                fontSize: 15,
                color: AppColors.onErrorContainer,
              ),
            ),
            const SizedBox(height: 4),
          ],
          if (amountMinor != null && currency != null) ...[
            Text(
              formatQrMinor(amountMinor!, currency!),
              style: AppTypography.titleMd.copyWith(
                color: AppColors.onErrorContainer,
              ),
            ),
            const SizedBox(height: 4),
          ],
          Text(
            failure.message,
            key: const Key('qr-pay-error-message'),
            textAlign: TextAlign.center,
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onErrorContainer,
            ),
          ),
          const SizedBox(height: 12),
          if (onRetry != null)
            SizedBox(
              width: double.infinity,
              height: 48,
              child: FilledButton(
                key: const Key('qr-pay-retry'),
                onPressed: onRetry,
                child: const Text('Reintentar'),
              ),
            ),
          if (onRescan != null) ...[
            if (onRetry != null) const SizedBox(height: 8),
            SizedBox(
              width: double.infinity,
              height: 48,
              child: OutlinedButton.icon(
                key: const Key('qr-pay-rescan'),
                onPressed: onRescan,
                icon: const Icon(Icons.qr_code_scanner, size: 20),
                label: const Text('Re-escanear'),
              ),
            ),
          ],
        ],
      ),
    );
  }
}
