import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../payment_authorization/data/payment_authorization_service.dart';
import '../../payment_authorization/payment_authorization_controller.dart';
import '../data/services_service.dart';
import '../data/wallet_service.dart';
import '../models/services.dart';
import '../models/wallet.dart';
import 'service_receipt_card.dart';

/// Recarga de celular con comprobante (E4-T19, HU16 CA-02/CA-04).
///
/// Flujo (cliente delgado estricto, docs/19 §4): operador + numero +
/// monto + fuente (`GET /wallet`) -> `POST /topups` + `Idempotency-Key`
/// (el Reintentar reutiliza la misma clave) -> comprobante del servidor
/// (`receipt` con `receipt_no` oficial). Sobre el limite: biometria o PIN
/// (patron HU15) + replay con la misma clave.
///
/// Estados (docs/20 §7): contenido / carga / vacio / error con
/// Reintentar; mensajes en español; sin logica offline.
class TopupPage extends StatefulWidget {
  const TopupPage({
    super.key,
    this.servicesService,
    this.walletService,
    this.authController,
    this.authService,
    this.onDone,
  });

  final ServicesServiceBase? servicesService;
  final WalletServiceBase? walletService;
  final PaymentAuthorizationController? authController;
  final PaymentAuthorizationServiceBase? authService;
  final VoidCallback? onDone;

  @override
  State<TopupPage> createState() => _TopupPageState();
}

enum _Stage { loading, form, paying, pendingAuth, receipt, rejected, failed }

class _TopupPageState extends State<TopupPage> {
  final String _topupKey = const Uuid().v4();

  _Stage _stage = _Stage.loading;
  List<WalletLink> _links = [];
  String? _selectedLinkId;
  String? _linksError;
  List<Biller> _operators = [];

  String? _selectedOperator;
  final TextEditingController _phone = TextEditingController();
  final TextEditingController _amount = TextEditingController();
  String? _formError;

  ServiceOperationResult? _result;
  ServiceFailure? _failure;
  String? _authMessage;
  bool _authorizing = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _phone.dispose();
    _amount.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    final walletService = widget.walletService;
    final servicesService = widget.servicesService;
    if (walletService == null || servicesService == null) {
      if (!mounted) return;
      setState(() {
        _stage = _Stage.form;
        _linksError = 'Servicios de billetera o recargas no configurados.';
      });
      return;
    }
    try {
      final wallet = await walletService.getWallet();
      final catalog = await servicesService.catalog(category: 'TELEFONIA');
      if (!mounted) return;
      final links =
          wallet.links.where((l) => l.isActive && l.isAccount).toList();
      setState(() {
        _links = links;
        _selectedLinkId = links.firstOrNull?.id;
        _operators = catalog;
        _selectedOperator = catalog.firstOrNull?.billerId;
        _stage = _Stage.form;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _stage = _Stage.form;
        _linksError = e.message;
        _failure = serviceFailureFor(code: e.code, fallback: e.message);
      });
    }
  }

  /// Monto en centimos desde texto `10.00` (parseo de presentacion;
  /// rechaza mas de 2 decimales sin truncar en silencio, H2).
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

  Future<void> _topup() async {
    final service = widget.servicesService;
    final operatorId = _selectedOperator;
    final phone = _phone.text.trim();
    final minor = _parseMinor(_amount.text);
    if (service == null) {
      setState(() {
        _failure = const ServiceFailure(
          message: 'Servicio de recargas no configurado.',
          retry: false,
        );
        _stage = _Stage.failed;
      });
      return;
    }
    if (operatorId == null || operatorId.isEmpty) {
      setState(() => _formError = 'Elige el operador para recargar.');
      return;
    }
    if (!RegExp(r'^\d{6,15}$').hasMatch(phone)) {
      setState(() {
        _formError = 'Ingresa un número válido (6 a 15 dígitos).';
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
          _formError = 'Ingresa un monto válido mayor a cero (ej. 10.00).';
        });
      }
      return;
    }
    final linkId = _selectedLinkId;
    if (linkId == null || linkId.isEmpty) {
      setState(() => _formError = 'Elige la fuente de fondos.');
      return;
    }
    setState(() {
      _formError = null;
      _failure = null;
      _stage = _Stage.paying;
    });
    try {
      final result = await service.topup(
        operatorRef: operatorId,
        phone: phone,
        amountMinor: minor,
        linkId: linkId,
        idempotencyKey: _topupKey,
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
        _failure = serviceFailureFor(code: e.code, fallback: e.message);
        _stage = _Stage.failed;
      });
    }
  }

  Future<void> _authorizeBiometric() async {
    final controller = widget.authController;
    final result = _result;
    if (controller == null || result == null) {
      setState(() {
        _authMessage = 'Biometría no configurada en este dispositivo.';
      });
      return;
    }
    final nonce = result.authNonce ?? '';
    if (nonce.isEmpty) {
      setState(() {
        _authMessage = 'El servidor no envió el reto de autorización.';
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
        idempotencyKey: paymentAuthInitialKey(_topupKey),
        reason: 'Autoriza la recarga con tu biometría',
      );
      if (!mounted) return;
      if (state.outcome == PaymentAuthOutcome.authorized) {
        await _finalizeAfterAuth();
        return;
      }
      setState(() {
        _failure = serviceFailureFor(
          code: state.code ?? 'UNKNOWN',
          fallback: state.message.isEmpty
              ? 'No pudimos autorizar la recarga.'
              : state.message,
        );
        _stage = _Stage.failed;
      });
    } finally {
      if (mounted) setState(() => _authorizing = false);
    }
  }

  Future<void> _finalizeAfterAuth() async {
    final service = widget.servicesService;
    final minor = _parseMinor(_amount.text);
    final linkId = _selectedLinkId;
    final operatorId = _selectedOperator;
    if (service == null ||
        operatorId == null ||
        minor == null ||
        linkId == null ||
        linkId.isEmpty) {
      if (!mounted) return;
      setState(() {
        _authorizing = false;
        _failure = const ServiceFailure(
          message: 'No pudimos finalizar tu recarga. Inténtalo de nuevo.',
          retry: true,
        );
        _stage = _Stage.failed;
      });
      return;
    }
    try {
      final finalResult = await service.topup(
        operatorRef: operatorId,
        phone: _phone.text.trim(),
        amountMinor: minor,
        linkId: linkId,
        idempotencyKey: _topupKey,
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
          _stage = _Stage.rejected;
        });
      } else {
        setState(() {
          _result = finalResult;
          _failure = serviceFailureFor(
            code: 'UNKNOWN',
            fallback: 'El servidor respondió un estado no reconocido.',
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
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Recargar celular')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        child: _buildStage(),
      ),
    );
  }

  Widget _buildStage() {
    switch (_stage) {
      case _Stage.loading:
        return const Center(
          child: Padding(
            padding: EdgeInsets.symmetric(vertical: 48),
            child:
                CircularProgressIndicator(key: Key('topup-loading')),
          ),
        );
      case _Stage.form:
        return _buildForm();
      case _Stage.paying:
        return const Center(
          child: Padding(
            padding: EdgeInsets.symmetric(vertical: 48),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                CircularProgressIndicator(key: Key('topup-paying')),
                SizedBox(height: 12),
                Text('Procesando tu recarga…'),
              ],
            ),
          ),
        );
      case _Stage.pendingAuth:
        return _buildPendingAuth();
      case _Stage.receipt:
        final result = _result;
        if (result == null) return _buildForm();
        return ServiceReceiptCard(
          result: result,
          title: 'Recarga exitosa',
          onDone: _done,
        );
      case _Stage.rejected:
        final result = _result;
        return ServiceErrorCard(
          failure: serviceFailureFor(
            code: 'UNKNOWN',
            failureCode: result?.failureCode,
            fallback: 'La recarga fue rechazada. No se descontó nada.',
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
          onRetry: (_failure?.retry ?? true) ? _topup : null,
          onBack: () => setState(() => _stage = _Stage.form),
        );
    }
  }

  Widget _buildForm() {
    if (_linksError != null && _links.isEmpty) {
      return ServiceErrorCard(
        failure: _failure ??
            ServiceFailure(message: _linksError!, retry: true),
        onRetry: () {
          setState(() => _stage = _Stage.loading);
          _load();
        },
      );
    }
    if (_links.isEmpty) {
      return EmptyView(
        key: const Key('topup-empty-links'),
        message: 'Aún no tienes una fuente de fondos para recargar.\n'
            'Vincula una cuenta del banco para continuar.',
        actionLabel: 'Vincular fuente',
        onAction: () => context.push('/wallet/link'),
      );
    }
    return AppCard(
      key: const Key('topup-form'),
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Recarga del servidor', style: AppTypography.titleMd),
          const SizedBox(height: 4),
          Text(
            'El operador y el comprobante los confirma el servidor.',
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: 12),
          DropdownButtonFormField<String>(
            key: const Key('topup-operator-field'),
            initialValue: _selectedOperator,
            decoration: InputDecoration(
              labelText: 'Operador',
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
            items: [
              for (final op in _operators)
                DropdownMenuItem(value: op.billerId, child: Text(op.name)),
            ],
            onChanged: (v) => setState(() => _selectedOperator = v),
          ),
          const SizedBox(height: 8),
          TextField(
            key: const Key('topup-phone-field'),
            controller: _phone,
            keyboardType: TextInputType.phone,
            decoration: InputDecoration(
              labelText: 'Número de celular',
              hintText: 'Ej. 987654321',
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
          ),
          const SizedBox(height: 8),
          TextField(
            key: const Key('topup-amount-field'),
            controller: _amount,
            keyboardType:
                const TextInputType.numberWithOptions(decimal: true),
            decoration: InputDecoration(
              labelText: 'Monto (S/)',
              hintText: 'Ej. 10.00',
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
          ),
          const SizedBox(height: 8),
          DropdownButtonFormField<String>(
            key: const Key('topup-link-field'),
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
              key: const Key('topup-form-error'),
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
              key: const Key('topup-confirm'),
              onPressed: _topup,
              icon: const Icon(Icons.smartphone_outlined, size: 20),
              label: const Text(
                'Confirmar recarga',
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
    return AppCard(
      key: const Key('topup-pending-card'),
      padding: const EdgeInsets.all(20),
      child: Column(
        children: [
          Text(
            formatServiceMinor(result.amountMinor, result.currency),
            style: AppTypography.displayLg.copyWith(
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            'Este monto supera el límite base: confirma con tu biometría.',
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
              key: const Key('topup-auth-message'),
              textAlign: TextAlign.center,
              style: AppTypography.labelMd.copyWith(fontSize: 13),
            ),
          ],
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            height: 52,
            child: FilledButton.icon(
              key: const Key('topup-authorize-biometric'),
              onPressed: _authorizing ? null : _authorizeBiometric,
              icon: const Icon(Icons.fingerprint, size: 20),
              label: const Text('Autorizar con biometría'),
            ),
          ),
        ],
      ),
    );
  }
}
