import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/error_view.dart';
import '../../payment_authorization/data/payment_authorization_service.dart';
import '../../payment_authorization/payment_authorization_controller.dart';
import '../../payment_authorization/payment_authorization_routes.dart';
import '../../payment_authorization/presentation/biometric_challenge_sheet.dart';
import '../../payment_authorization/presentation/payment_error_page.dart';
import '../data/transfers_service.dart';
import '../models/transfer.dart';
import 'tracking_page.dart';
import 'widgets/cci_status_banner.dart';

/// Confirmacion de transferencia (mockup `Confirmación de Transferencia`).
///
/// Muestra el resumen y ejecuta el POST con la `Idempotency-Key` del
/// borrador; el reintento reutiliza la MISMA clave. El total exhibido viene
/// del backend (`amount + fee` de la respuesta; el cliente no calcula).
///
/// Destinos tras el intento:
/// - propia/tercero `SETTLED` -> `/transfers/receipt/:id`.
/// - interbancaria aceptada (`202`) -> `/transfers/tracking/:id`.
/// - `REJECTED` por fondos -> tarjeta de saldo insuficiente (mockup
///   `Error Pago 1`; sin debitar, reintentable con otra cuenta).
/// - `REJECTED` por limite diario o bloqueo preventivo -> estados de error
///   de pago 2/3 (E2-T21, sin tecnicismos).
/// - `PENDING_AUTHORIZATION` -> reto biometrico del dispositivo (E2-T21):
///   bottom sheet `Autorización Biométrica`, firma del `nonce` y
///   `POST /transactions/{id}/authorize` con clave DERIVADA
///   `<borrador>-auth` (la UQ `(key, user_id)` es global: reutilizar la del
///   borrador da `409`); `409` -> reconcilia con `GET /transactions/{id}`
///   (exito -> comprobante, pendiente -> reintento con clave nueva + PIN,
///   terminal -> error); fallo -> mensaje claro + fallback a PIN;
///   `EXPIRED_NONCE` -> rotacion del reto + reintento con clave nueva
///   (lo hace el controlador).
/// - `HOLDER_NOT_FOUND` / `HOLDER_LOOKUP_UNAVAILABLE` (interbancaria) ->
///   banner CCI + [Corregir / Reintentar] (misma clave).
class ConfirmPage extends StatefulWidget {
  const ConfirmPage({
    super.key,
    required this.args,
    this.service,
    this.authDepsFactory,
  });

  final TransferFlowArgs args;
  final TransfersServiceBase? service;

  /// Fabrica del reto biometrico (en produccion resuelve la global
  /// `paymentAuthorizationDepsFactory`; en tests se inyecta un fake).
  final PaymentAuthorizationDepsFactory? authDepsFactory;

  @override
  State<ConfirmPage> createState() => _ConfirmPageState();
}

class _ConfirmPageState extends State<ConfirmPage> {
  bool _submitting = false;
  ApiException? _error;
  TransferResult? _rejected;
  CciState _cciState = CciState.idle;
  final String _holderMasked = '';

  /// Estado de error de pago E2-T21 (mockups 2..6; el 1 conserva su vista
  /// dedicada `_insufficientView`).
  PaymentErrorArgs? _paymentError;

  /// Mensaje del fallback a PIN tras el reto (borde del brief E2-T21).
  String? _pinFallbackMessage;

  TransferDraft get _draft => widget.args.draft;

  Future<void> _submit() async {
    final service = widget.service;
    if (service == null) {
      setState(() {
        _error = ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de transferencias no configurado.',
        );
      });
      return;
    }
    setState(() {
      _submitting = true;
      _error = null;
      _rejected = null;
      _cciState = CciState.idle;
      _paymentError = null;
      _pinFallbackMessage = null;
    });
    try {
      final result = await _post(service);
      if (!mounted) return;
      if (result.isInsufficientFunds) {
        setState(() {
          _submitting = false;
          _rejected = result;
        });
        return;
      }
      if (result.requiresBiometricChallenge) {
        await _startBiometricChallenge(result);
        return;
      }
      if (result.isRejected) {
        final kind = PaymentErrorKind.fromCode(result.failureCode);
        if (result.failureCode == 'DAILY_LIMIT_EXCEEDED' ||
            result.failureCode == 'BIOMETRIC_PREVENTIVE_BLOCK') {
          setState(() {
            _submitting = false;
            _paymentError = _errorArgs(
              kind,
              transactionId: result.transactionId,
            );
          });
          return;
        }
        setState(() {
          _submitting = false;
          _error = ApiException(
            code: result.failureCode ?? 'UNKNOWN',
            message:
                'La transferencia fue rechazada (${result.failureCode ?? 'sin motivo'}). '
                'No se debitó nada.',
          );
        });
        return;
      }
      if (_draft.kind == TransferKind.interbank) {
        context.pushReplacement(
          '/transfers/tracking/${result.transactionId}',
          extra: TrackingArgs(
            transactionId: result.transactionId,
            holderMasked: result.holderMasked,
            amountMinor: result.amountMinor,
            currency: result.currency,
          ),
        );
      } else {
        context.pushReplacement('/transfers/receipt/${result.transactionId}');
      }
      if (mounted) setState(() => _submitting = false);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _submitting = false;
        if (e.code == 'HOLDER_NOT_FOUND') {
          _cciState = CciState.notFound;
        } else if (e.code == 'HOLDER_LOOKUP_UNAVAILABLE' ||
            e.code == 'HOLDER_LOOKUP_TIMEOUT' ||
            e.code == 'TIMEOUT_ERROR') {
          _cciState = CciState.unavailable;
        } else {
          _error = e;
        }
      });
    }
  }

  Future<TransferResult> _post(TransfersServiceBase service) {
    final d = _draft;
    final motiveMeta = d.motive.isEmpty
        ? null
        : <String, dynamic>{'motive': d.motive};
    switch (d.kind) {
      case TransferKind.own:
        return service.createOwnTransfer(
          sourceAccountId: d.sourceAccountId,
          targetAccountId: d.targetAccountId ?? '',
          amountMinor: d.amountMinor,
          currency: d.currency,
          metadata: motiveMeta,
          idempotencyKey: d.idempotencyKey,
        );
      case TransferKind.thirdParty:
        return service.createThirdPartyTransfer(
          sourceAccountId: d.sourceAccountId,
          targetAccountId: d.targetAccountId,
          amountMinor: d.amountMinor,
          currency: d.currency,
          beneficiaryId: d.beneficiaryId,
          metadata: motiveMeta,
          idempotencyKey: d.idempotencyKey,
        );
      case TransferKind.interbank:
        return service.createInterbankTransfer(
          sourceAccountId: d.sourceAccountId,
          amountMinor: d.amountMinor,
          cci: d.cci,
          currency: d.currency,
          holderName: d.holderName.isEmpty ? null : d.holderName,
          bankCode: d.bankCode.isEmpty ? null : d.bankCode,
          metadata: motiveMeta,
          idempotencyKey: d.idempotencyKey,
        );
    }
  }

  void _correctCci() {
    if (_draft.kind == TransferKind.interbank) {
      context.pop(CciOutcome(_cciState));
    } else {
      context.pop();
    }
  }

  /// Monto exhibido del borrador (viene del servidor al confirmar; aqui
  /// solo display del borrador capturado).
  String get _amountLabel =>
      formatMoneyMinor(_draft.amountMinor, _draft.currency);

  /// Destinatario enmascarado (alias de la agenda o mascara; nunca PII).
  String get _recipientLabel {
    final d = _draft;
    if (d.beneficiaryAlias.isNotEmpty) return d.beneficiaryAlias;
    if (d.destinationMasked.isNotEmpty) return d.destinationMasked;
    if (d.kind == TransferKind.interbank && d.cci.isNotEmpty) {
      return maskCciShort(d.cci);
    }
    return 'Destinatario';
  }

  /// Argumentos de [PaymentErrorView] con el resumen del intento.
  PaymentErrorArgs _errorArgs(
    PaymentErrorKind kind, {
    String? transactionId,
    String? errorCode,
  }) {
    final d = _draft;
    final bankMasked = d.kind == TransferKind.interbank
        ? (d.bankCode.isEmpty ? 'Otro banco (CCE)' : d.bankCode)
        : d.destinationMasked;
    return PaymentErrorArgs(
      kind: kind,
      errorCode: errorCode,
      recipientLabel: _recipientLabel,
      bankMasked: bankMasked,
      amountLabel: _amountLabel,
      totalLabel: _amountLabel,
      reportRef: transactionId ?? '',
    );
  }

  /// Reto biometrico tras `PENDING_AUTHORIZATION` (E2-T21).
  ///
  /// Muestra el bottom sheet `Autorización Biométrica` (mockup `0:3964`);
  /// con la huella superada firma el `nonce` y autoriza con clave derivada
  /// `<borrador>-auth` (nunca la del borrador: la UQ es global y el reuso
  /// cruzado da `409`). Sin biometria o ante fallo/cancelacion ofrece el
  /// fallback a PIN; `EXPIRED_NONCE` rota el reto y reintenta con clave
  /// nueva (lo hace el controlador); `409` reconcilia con
  /// `GET /transactions/{id}` antes de mostrar errores.
  Future<void> _startBiometricChallenge(TransferResult pending) async {
    final factory = widget.authDepsFactory ?? paymentAuthorizationDepsFactory;
    if (factory == null) {
      if (!mounted) return;
      setState(() {
        _submitting = false;
        _error = ApiException(
          code: 'UNKNOWN',
          message:
              'Autorización biométrica no disponible en este momento. '
              'Inténtalo más tarde.',
        );
      });
      return;
    }
    final deps = factory();
    var nonce = pending.nonce ?? '';
    if (nonce.isEmpty) {
      try {
        final detail = await deps.service.refreshChallenge(
          pending.transactionId,
        );
        nonce = detail.nonce ?? '';
      } on ApiException catch (e) {
        if (!mounted) return;
        setState(() {
          _submitting = false;
          _error = e;
        });
        return;
      }
    }
    if (!mounted) return;
    if (nonce.isEmpty) {
      setState(() {
        _submitting = false;
        _paymentError = _errorArgs(
          PaymentErrorKind.sessionExpired,
          transactionId: pending.transactionId,
        );
      });
      return;
    }
    final controller = deps.buildController();
    final reason =
        'Autoriza tu transferencia de $_amountLabel a $_recipientLabel';
    PaymentAuthState? authState;
    AuthorizationResult? conflictSettled;
    TransactionDetail? conflictTerminal;
    // Clave del `authorize` DERIVADA del borrador: la UQ `(key, user_id)`
    // es global sin `endpoint` y reutilizar la del `POST /transfers/...`
    // con cuerpo distinto devuelve `409` (el reto nunca se ejecutaria).
    var currentAuthKey =
        paymentAuthInitialKey(_draft.idempotencyKey) ?? _draft.idempotencyKey;
    var sheetBusy = false;
    var sheetError = '';

    Future<TransactionDetail?> fetchDetail() async {
      final svc = widget.service;
      if (svc == null) return null;
      try {
        return await svc.getTransaction(pending.transactionId);
      } on ApiException {
        return null;
      }
    }

    AuthorizationResult settledFromDetail(TransactionDetail detail) {
      return AuthorizationResult(
        transactionId: detail.transactionId,
        status: detail.status,
        tracking: detail.tracking,
        failureCode: detail.failureCode,
      );
    }

    bool isTerminalFailure(TransactionDetail detail) {
      final s = detail.status.toUpperCase();
      return s == 'REJECTED' || s == 'FAILED';
    }

    void popSheet(BuildContext sheetContext, BiometricSheetOutcome outcome) {
      if (sheetContext.mounted) {
        Navigator.of(sheetContext).pop(outcome);
      }
    }

    Future<void> runAttempt(
      StateSetter setSheet,
      BuildContext sheetContext,
    ) async {
      setSheet(() {
        sheetBusy = true;
        sheetError = '';
      });
      final st = await controller.authorize(
        transactionId: pending.transactionId,
        nonce: nonce,
        idempotencyKey: currentAuthKey,
        reason: reason,
      );
      if (!sheetContext.mounted) return;
      if (st.outcome == PaymentAuthOutcome.authorized) {
        authState = st;
        popSheet(sheetContext, BiometricSheetOutcome.authorize);
        return;
      }
      if (st.outcome == PaymentAuthOutcome.requiresPinFallback) {
        authState = st;
        popSheet(sheetContext, BiometricSheetOutcome.usePin);
        return;
      }
      if (st.outcome == PaymentAuthOutcome.expired) {
        authState = st;
        popSheet(sheetContext, BiometricSheetOutcome.authorize);
        return;
      }
      if (!isPaymentAuthConflictCode(st.code)) {
        setSheet(() {
          sheetBusy = false;
          sheetError = st.message;
        });
        authState = st;
        return;
      }
      // `409` por reuso cruzado de la clave: reconciliar con el estado
      // real antes de mostrar "ya procesada" como callejon sin salida.
      final detail = await fetchDetail();
      if (!sheetContext.mounted) return;
      if (detail == null) {
        setSheet(() {
          sheetBusy = false;
          sheetError = st.message;
        });
        authState = st;
        return;
      }
      if (detail.isSuccess) {
        conflictSettled = settledFromDetail(detail);
        authState = st;
        popSheet(sheetContext, BiometricSheetOutcome.authorize);
        return;
      }
      if (detail.status.toUpperCase() == 'PENDING_AUTHORIZATION') {
        final nextKey =
            paymentAuthRetryKey(currentAuthKey) ?? '$currentAuthKey-retry-1';
        currentAuthKey = nextKey;
        var retryNonce = (detail.nonce?.isNotEmpty ?? false)
            ? detail.nonce!
            : nonce;
        if (retryNonce.isEmpty) {
          try {
            final fresh = await deps.service.refreshChallenge(
              pending.transactionId,
            );
            if ((fresh.nonce?.isNotEmpty ?? false)) {
              retryNonce = fresh.nonce!;
            }
          } on ApiException {
            // Sin reto vigente: ir al PIN en vez de bloquear.
          }
          if (!sheetContext.mounted) return;
        }
        if (retryNonce.isEmpty) {
          authState = st;
          popSheet(sheetContext, BiometricSheetOutcome.usePin);
          return;
        }
        final st2 = await controller.authorize(
          transactionId: pending.transactionId,
          nonce: retryNonce,
          idempotencyKey: nextKey,
          reason: reason,
        );
        if (!sheetContext.mounted) return;
        if (st2.outcome == PaymentAuthOutcome.authorized) {
          authState = st2;
          popSheet(sheetContext, BiometricSheetOutcome.authorize);
          return;
        }
        if (st2.outcome == PaymentAuthOutcome.requiresPinFallback) {
          authState = st2;
          popSheet(sheetContext, BiometricSheetOutcome.usePin);
          return;
        }
        if (st2.outcome == PaymentAuthOutcome.expired) {
          authState = st2;
          popSheet(sheetContext, BiometricSheetOutcome.authorize);
          return;
        }
        if (isPaymentAuthConflictCode(st2.code)) {
          final again = await fetchDetail();
          if (!sheetContext.mounted) return;
          if (again != null && again.isSuccess) {
            conflictSettled = settledFromDetail(again);
            authState = st2;
            popSheet(sheetContext, BiometricSheetOutcome.authorize);
            return;
          }
          if (again != null && isTerminalFailure(again)) {
            conflictTerminal = again;
            authState = st2;
            popSheet(sheetContext, BiometricSheetOutcome.authorize);
            return;
          }
          // Sigue pendiente tras el segundo `409`: ofrecer PIN.
          authState = st2;
          popSheet(sheetContext, BiometricSheetOutcome.usePin);
          return;
        }
        setSheet(() {
          sheetBusy = false;
          sheetError = st2.message;
        });
        authState = st2;
        return;
      }
      if (isTerminalFailure(detail)) {
        conflictTerminal = detail;
        authState = st;
        popSheet(sheetContext, BiometricSheetOutcome.authorize);
        return;
      }
      setSheet(() {
        sheetBusy = false;
        sheetError = st.message;
      });
      authState = st;
    }

    // El sheet trae su propio estado de carga: se apaga el spinner de la
    // confirmacion para no animar tras el modal (ni bloquear el hilo).
    if (mounted) setState(() => _submitting = false);
    final sheetOutcome =
        await showModalBottomSheet<BiometricSheetOutcome>(
          context: context,
          isDismissible: true,
          enableDrag: true,
          backgroundColor: AppColors.surfaceContainerLowest,
          shape: const RoundedRectangleBorder(
            borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
          ),
          builder: (sheetContext) => StatefulBuilder(
            builder: (context, setSheet) => BiometricChallengeSheet(
              amountLabel: _amountLabel,
              recipientLabel: _recipientLabel,
              isAuthorizing: sheetBusy,
              errorMessage: sheetError,
              onAuthorize: () => runAttempt(setSheet, sheetContext),
              onRetry: () => runAttempt(setSheet, sheetContext),
              onUsePin: () =>
                  Navigator.of(sheetContext).pop(BiometricSheetOutcome.usePin),
              onCancel: () => Navigator.of(
                sheetContext,
              ).pop(BiometricSheetOutcome.cancelled),
            ),
          ),
        );
    if (!mounted) return;
    if (conflictSettled != null) {
      _continueAfterAuthorization(conflictSettled!, pending);
      return;
    }
    if (conflictTerminal != null) {
      final detail = conflictTerminal!;
      setState(() {
        _submitting = false;
        _paymentError = _errorArgs(
          PaymentErrorKind.fromCode(detail.failureCode),
          transactionId: detail.transactionId,
          errorCode: detail.failureCode,
        );
      });
      return;
    }
    final st = authState;
    // `409` sin resolver dentro del sheet (p. ej. fallo el `GET`):
    // reconciliar una vez fuera antes de mostrar el duplicado.
    if (st != null && isPaymentAuthConflictCode(st.code)) {
      final detail = await fetchDetail();
      if (!mounted) return;
      if (detail != null && detail.isSuccess) {
        _continueAfterAuthorization(settledFromDetail(detail), pending);
        return;
      }
      if (detail != null &&
          detail.status.toUpperCase() == 'PENDING_AUTHORIZATION') {
        setState(() {
          _submitting = false;
          _pinFallbackMessage =
              'La operación sigue pendiente. Puedes reintentar con tu '
              'huella o continuar con tu PIN de seguridad.';
        });
        return;
      }
      if (detail != null && isTerminalFailure(detail)) {
        setState(() {
          _submitting = false;
          _paymentError = _errorArgs(
            PaymentErrorKind.fromCode(detail.failureCode),
            transactionId: detail.transactionId,
            errorCode: detail.failureCode,
          );
        });
        return;
      }
    }
    if (st == null) {
      // El tap directo en "Usar mi PIN" cierra el sheet con `usePin` pero
      // sin pasar por el controlador: igual debe montar el fallback.
      if (sheetOutcome == BiometricSheetOutcome.usePin) {
        setState(() {
          _submitting = false;
          _pinFallbackMessage =
              'No pudimos verificar tu huella. Puedes reintentar o usar '
              'tu PIN de seguridad.';
        });
        return;
      }
      setState(() => _submitting = false);
      return;
    }
    if (st.outcome == PaymentAuthOutcome.authorized && st.result != null) {
      _continueAfterAuthorization(st.result!, pending);
      return;
    }
    if (st.outcome == PaymentAuthOutcome.expired) {
      setState(() {
        _submitting = false;
        _paymentError = _errorArgs(
          PaymentErrorKind.sessionExpired,
          transactionId: pending.transactionId,
          errorCode: st.code,
        );
      });
      return;
    }
    if (st.outcome == PaymentAuthOutcome.requiresPinFallback ||
        sheetOutcome == BiometricSheetOutcome.usePin) {
      setState(() {
        _submitting = false;
        _pinFallbackMessage = st.message.isNotEmpty
            ? st.message
            : 'No pudimos verificar tu huella. Puedes reintentar o usar '
                  'tu PIN de seguridad.';
      });
      return;
    }
    // Fallo comunicado dentro del sheet con reintento: si el usuario cerro
    // el sheet con error pendiente, se mapea al estado de error de pago.
    final kind = PaymentErrorKind.fromCode(st.code);
    if (kind == PaymentErrorKind.timeout ||
        kind == PaymentErrorKind.duplicate ||
        kind == PaymentErrorKind.preventiveBlock ||
        kind == PaymentErrorKind.dailyLimit) {
      setState(() {
        _submitting = false;
        _paymentError = _errorArgs(
          kind,
          transactionId: pending.transactionId,
          errorCode: st.code,
        );
      });
      return;
    }
    setState(() {
      _submitting = false;
      _error = ApiException(code: st.code ?? 'UNKNOWN', message: st.message);
    });
  }

  /// Continua el flujo tras la autorizacion aceptada (E2-T21 CA-01: sin
  /// exito del reto no se continua).
  void _continueAfterAuthorization(
    AuthorizationResult auth,
    TransferResult pending,
  ) {
    if (auth.isPreventiveBlock) {
      setState(() {
        _submitting = false;
        _paymentError = _errorArgs(
          PaymentErrorKind.preventiveBlock,
          transactionId: auth.transactionId,
          errorCode: auth.failureCode,
        );
      });
      return;
    }
    if (_draft.kind == TransferKind.interbank) {
      final holder = auth.holderMasked.isEmpty
          ? pending.holderMasked
          : auth.holderMasked;
      context.pushReplacement(
        '/transfers/tracking/${auth.transactionId}',
        extra: TrackingArgs(
          transactionId: auth.transactionId,
          holderMasked: holder,
          amountMinor: pending.amountMinor,
          currency: pending.currency,
        ),
      );
    } else {
      context.pushReplacement('/transfers/receipt/${auth.transactionId}');
    }
    if (mounted) setState(() => _submitting = false);
  }

  /// Acciones de cada estado de error de pago (mockups 1..6).
  void _onPaymentPrimary(PaymentErrorKind kind, String transactionId) {
    switch (kind) {
      case PaymentErrorKind.insufficientFunds:
        context.pop();
      case PaymentErrorKind.dailyLimit:
        context.go('/profile');
      case PaymentErrorKind.preventiveBlock:
        context.go('/login');
      case PaymentErrorKind.sessionExpired:
        setState(() {
          _paymentError = null;
          _pinFallbackMessage = null;
        });
        _submit();
      case PaymentErrorKind.timeout:
        context.go('/movements');
      case PaymentErrorKind.duplicate:
        context.pushReplacement('/transfers/receipt/$transactionId');
    }
  }

  void _onPaymentSecondary(PaymentErrorKind kind) {
    switch (kind) {
      case PaymentErrorKind.insufficientFunds:
      case PaymentErrorKind.sessionExpired:
      case PaymentErrorKind.preventiveBlock:
      case PaymentErrorKind.dailyLimit:
      case PaymentErrorKind.timeout:
      case PaymentErrorKind.duplicate:
        context.go('/home');
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_rejected != null) return _insufficientView(_rejected!);
    if (_paymentError != null) return _paymentErrorView(_paymentError!);
    if (_pinFallbackMessage != null) {
      return _pinFallbackView(_pinFallbackMessage!);
    }
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Confirma tu transferencia')),
      body: Column(
        children: [
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
              children: [
                if (_cciState == CciState.notFound ||
                    _cciState == CciState.unavailable) ...[
                  CciStatusBanner(
                    state: _cciState,
                    holderMasked: _holderMasked,
                    onRetry: _cciState == CciState.unavailable ? _submit : null,
                    retryKey: const Key('confirm-retry'),
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    width: double.infinity,
                    child: OutlinedButton(
                      onPressed: _correctCci,
                      child: const Text('Corregir datos'),
                    ),
                  ),
                  const SizedBox(height: 16),
                ],
                if (_error != null) ...[
                  ErrorView(
                    key: const Key('confirm-error'),
                    message: transferUserMessage(_error!),
                    onRetry: _submit,
                    retryLabel: 'Reintentar',
                  ),
                  const SizedBox(height: 16),
                ],
                _Hero(draft: _draft),
                const SizedBox(height: 16),
                _SummaryCard(args: widget.args),
                const SizedBox(height: 16),
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: AppColors.warningContainer,
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.warning_amber,
                        size: 20,
                        color: AppColors.onWarningContainer,
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Text(
                          'Revisa los datos. Las transferencias no se pueden revertir.',
                          style: AppTypography.bodyMd.copyWith(
                            fontSize: 13,
                            color: AppColors.onSurface,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  SizedBox(
                    width: double.infinity,
                    child: FilledButton(
                      key: const Key('confirm-submit'),
                      onPressed: _submitting ? null : _submit,
                      style: FilledButton.styleFrom(
                        minimumSize: const Size(88, 52),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(AppRadii.md),
                        ),
                      ),
                      child: _submitting
                          ? const SizedBox.square(
                              key: Key('confirm-loading'),
                              dimension: 20,
                              child: CircularProgressIndicator(
                                strokeWidth: 2.5,
                                color: AppColors.onPrimary,
                              ),
                            )
                          : const Text('Confirmar transferencia'),
                    ),
                  ),
                  TextButton(
                    key: const Key('confirm-cancel'),
                    onPressed: _submitting ? null : () => context.pop(),
                    child: const Text('Cancelar'),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  /// Estado de error de pago E2-T21 (mockups 2..6).
  Widget _paymentErrorView(PaymentErrorArgs args) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        leading: IconButton(
          key: const Key('payment-error-close'),
          icon: const Icon(Icons.close),
          onPressed: () => context.pop(),
        ),
        title: Text('Estado del pago · ${args.kind.position} de 6'),
        actions: const [
          Padding(
            padding: EdgeInsets.only(right: 16),
            child: CircleAvatar(
              radius: 16,
              backgroundColor: AppColors.primary,
              child: Icon(Icons.person_outline, size: 18, color: Colors.white),
            ),
          ),
        ],
      ),
      body: PaymentErrorView(
        args: args,
        onPrimary: () => _onPaymentPrimary(args.kind, args.reportRef),
        onSecondary: () => _onPaymentSecondary(args.kind),
      ),
    );
  }

  /// Fallback a PIN tras el reto (borde del brief E2-T21): mensaje claro +
  /// accion al login con PIN + reintento de la biometria.
  Widget _pinFallbackView(String message) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.close),
          onPressed: () => context.pop(),
        ),
        title: const Text('Autorización biométrica'),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          AppCard(
            key: const Key('pin-fallback-card'),
            padding: const EdgeInsets.all(20),
            child: Column(
              children: [
                Container(
                  width: 64,
                  height: 64,
                  alignment: Alignment.center,
                  decoration: const BoxDecoration(
                    color: Color(0xFFEAF0EA),
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(
                    Icons.fingerprint,
                    size: 32,
                    color: AppColors.primaryContainer,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  'Continúa con tu PIN',
                  style: AppTypography.titleMd.copyWith(
                    fontSize: 20,
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  message,
                  key: const Key('pin-fallback-message'),
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.onSurfaceVariant,
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              key: const Key('pin-fallback-go'),
              onPressed: () => context.go('/login'),
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Usar mi PIN de seguridad'),
            ),
          ),
          const SizedBox(height: 10),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('pin-fallback-retry'),
              onPressed: _submitting
                  ? null
                  : () {
                      setState(() => _pinFallbackMessage = null);
                      _submit();
                    },
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Reintentar con huella'),
            ),
          ),
        ],
      ),
    );
  }

  Widget _insufficientView(TransferResult result) {
    final draft = _draft;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.close),
          onPressed: () => context.pop(),
        ),
        title: const Text('Estado del pago'),
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          AppCard(
            key: const Key('insufficient-banner'),
            padding: const EdgeInsets.all(20),
            child: Column(
              children: [
                Container(
                  width: 64,
                  height: 64,
                  alignment: Alignment.center,
                  decoration: const BoxDecoration(
                    color: AppColors.errorContainer,
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(
                    Icons.account_balance_wallet_outlined,
                    size: 32,
                    color: AppColors.error,
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  'No tienes saldo suficiente',
                  textAlign: TextAlign.center,
                  style: AppTypography.titleMd.copyWith(
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: 8),
                Text(
                  'El monto más la comisión '
                  '(${formatMoneyMinor(result.totalMinor, result.currency)}) '
                  'supera tu saldo disponible.',
                  textAlign: TextAlign.center,
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.onSurfaceVariant,
                  ),
                ),
                const SizedBox(height: 12),
                Container(
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: AppColors.surfaceContainerLow,
                    borderRadius: BorderRadius.circular(AppRadii.def),
                  ),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.verified_outlined,
                        size: 18,
                        color: AppColors.primary,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          'Tu dinero está seguro. No se debitó nada.',
                          style: AppTypography.bodyMd.copyWith(
                            fontSize: 12,
                            color: AppColors.onSurfaceVariant,
                          ),
                        ),
                      ),
                    ],
                  ),
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
                  'TRANSFERENCIA PREVISTA',
                  style: AppTypography.labelMd.copyWith(
                    fontSize: 12,
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(height: 8),
                _kv(
                  'Destinatario',
                  draft.beneficiaryAlias.isEmpty
                      ? 'Destinatario'
                      : draft.beneficiaryAlias,
                ),
                _kv(
                  'Monto',
                  formatMoneyMinor(draft.amountMinor, draft.currency),
                ),
                _kv(
                  'Total intentado',
                  formatMoneyMinor(result.totalMinor, result.currency),
                  bold: true,
                ),
              ],
            ),
          ),
          const SizedBox(height: 16),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              onPressed: () => context.pop(),
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Elegir otra cuenta de origen'),
            ),
          ),
          const SizedBox(height: 8),
          Center(
            child: Text(
              'Transacción protegida CuyCash',
              style: AppTypography.bodyMd.copyWith(
                fontSize: 11,
                color: AppColors.onSurfaceVariant,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _kv(String k, String v, {bool bold = false}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(
            k,
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              color: AppColors.onSurfaceVariant,
            ),
          ),
          Text(
            v,
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              fontWeight: bold ? FontWeight.bold : FontWeight.w600,
              color: AppColors.onSurface,
            ),
          ),
        ],
      ),
    );
  }
}

class _Hero extends StatelessWidget {
  const _Hero({required this.draft});

  final TransferDraft draft;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Text(
          'Vas a enviar',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.onSurfaceVariant,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          formatMoneyMinor(draft.amountMinor, draft.currency),
          style: AppTypography.displayLg.copyWith(
            fontSize: 40,
            color: AppColors.onSurface,
          ),
        ),
        const SizedBox(height: 8),
        if (draft.kind == TransferKind.own)
          const AppStatusChip.success(label: 'SIN COMISIÓN')
        else
          const AppStatusChip.info(label: 'COMISIÓN LA CALCULA EL BANCO'),
      ],
    );
  }
}

class _SummaryCard extends StatelessWidget {
  const _SummaryCard({required this.args});

  final TransferFlowArgs args;

  @override
  Widget build(BuildContext context) {
    final draft = args.draft;
    final isInterbank = draft.kind == TransferKind.interbank;
    final destAccount = isInterbank
        ? maskCciShort(draft.cci)
        : draft.destinationMasked;
    final bank = isInterbank
        ? (draft.bankCode.isEmpty ? 'Otro banco (CCE)' : draft.bankCode)
        : 'CuyCash';
    return AppCard(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 8),
      child: Column(
        children: [
          _row(
            'Destinatario',
            draft.beneficiaryAlias.isEmpty ? '—' : draft.beneficiaryAlias,
          ),
          _row('Cuenta destino', destAccount.isEmpty ? '—' : destAccount),
          _row('Banco', bank),
          _row(
            'Desde',
            '${args.source.accountNumberMasked} · ${args.source.currency}',
          ),
          _row('Motivo', draft.motive.isEmpty ? '—' : draft.motive),
          _row(
            'Comisión',
            draft.kind == TransferKind.own
                ? formatMoneyMinor(0, draft.currency)
                : 'Calculada por el banco',
          ),
          _row('Fecha', 'Hoy'),
          Container(
            margin: const EdgeInsets.symmetric(vertical: 8),
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLow,
              borderRadius: BorderRadius.circular(AppRadii.def),
            ),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text(
                  'Total a debitar',
                  style: AppTypography.bodyLg.copyWith(
                    fontWeight: FontWeight.w600,
                    color: AppColors.onSurface,
                  ),
                ),
                Text(
                  formatMoneyMinor(draft.amountMinor, draft.currency),
                  style: AppTypography.titleMd.copyWith(
                    color: AppColors.onSurface,
                  ),
                ),
              ],
            ),
          ),
          if (draft.kind != TransferKind.own)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(
                'Más la comisión que calcule el banco (la verás en el comprobante).',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 12,
                  color: AppColors.outline,
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget _row(String k, String v) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        SizedBox(
          height: 52,
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                k,
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.onSurfaceVariant,
                ),
              ),
              Flexible(
                child: Text(
                  v,
                  textAlign: TextAlign.end,
                  style: AppTypography.bodyLg.copyWith(
                    fontSize: 15,
                    fontWeight: FontWeight.w500,
                    color: AppColors.onSurface,
                  ),
                ),
              ),
            ],
          ),
        ),
        const Divider(height: 1, color: AppColors.divider),
      ],
    );
  }
}
