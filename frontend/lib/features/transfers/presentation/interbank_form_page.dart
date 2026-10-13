import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../../accounts/models/account.dart';
import '../data/transfers_service.dart';
import '../models/transfer.dart';
import 'widgets/account_option_list.dart';
import 'widgets/cci_status_banner.dart';

/// Formulario interbancario CCI (mockups `Formulario Interbancario CCI` +
/// `CCI 2/3/4`, `pantallas.fig`).
///
/// Secciones Origen / Destino CCE / Monto + footer `Continuar a
/// confirmación`. La verificación del titular la ejecuta el backend al
/// confirmar (`POST /transfers/interbank`, E2-T08): este formulario valida
/// formato localmente (20 dígitos) y refleja los 4 estados del CCI con
/// [CciStatusBanner] según el resultado del intento.
///
/// El [_idempotencyKey] se genera una sola vez por formulario: continuar,
/// corregir y reintentar comparten la misma clave del intento en curso.
class InterbankFormPage extends StatefulWidget {
  const InterbankFormPage({super.key, this.service});

  final TransfersServiceBase? service;

  @override
  State<InterbankFormPage> createState() => _InterbankFormPageState();
}

class _InterbankFormPageState extends State<InterbankFormPage> {
  static const _banks = [
    'BCP',
    'INTERBANK',
    'BBVA',
    'SCOTIABANK',
    'OTRO',
  ];

  static const _quickChips = ['50', '100', '200', '500'];

  Future<List<Account>>? _future;
  String? _originId;
  String _bankCode = 'BCP';
  String _cci = '';
  String _amount = '';
  String _motive = '';
  String? _cciError;
  String? _amountError;
  CciState _cciState = CciState.idle;
  final String _holderMasked = '';
  final String _idempotencyKey = const Uuid().v4();

  final _cciController = TextEditingController();
  final _amountController = TextEditingController();
  final _motiveController = TextEditingController();

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  @override
  void dispose() {
    _cciController.dispose();
    _amountController.dispose();
    _motiveController.dispose();
    super.dispose();
  }

  Future<List<Account>> _load() {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de transferencias no configurado.',
        ),
      );
    }
    return service.accounts();
  }

  void _retryLoad() {
    setState(() {
      _originId = null;
      _future = _load();
    });
  }

  String get _digits => normalizeCci(_cci);

  int? get _amountMinor => parseAmountToMinor(_amount);

  bool get _valid {
    final minor = _amountMinor;
    return _originId != null &&
        isValidCciFormat(_cci) &&
        minor != null &&
        minor > 0 &&
        minor <= kMaxAmountMinor;
  }

  void _onCciChanged(String value) {
    setState(() {
      _cci = value;
      final digits = normalizeCci(value);
      if (digits.isEmpty) {
        _cciError = null;
      } else if (!RegExp(r'^[0-9]+$').hasMatch(digits)) {
        _cciError = 'El CCI solo admite dígitos (se ignoran espacios y guiones).';
      } else if (digits.length != 20) {
        _cciError = null; // Aun incompleto: lo muestra el contador.
      } else {
        _cciError = null;
      }
      if (_cciState == CciState.notFound ||
          _cciState == CciState.unavailable) {
        _cciState = CciState.idle;
      }
    });
  }

  void _onAmountChanged(String value) {
    setState(() {
      _amount = value;
      final minor = parseAmountToMinor(value);
      if (minor == null) {
        _amountError = 'Ingresa un monto válido (p. ej. 350.00).';
      } else if (minor <= 0) {
        _amountError = 'El monto debe ser mayor a S/ 0.00.';
      } else if (minor > kMaxAmountMinor) {
        _amountError = 'El monto supera el límite de S/ 30,000.';
      } else {
        _amountError = null;
      }
    });
  }

  Future<void> _continue(List<Account> accounts) async {
    if (!_valid) return;
    final source = accounts.firstWhere((a) => a.id == _originId);
    final draft = TransferDraft(
      kind: TransferKind.interbank,
      sourceAccountId: source.id,
      cci: _digits,
      bankCode: _bankCode == 'OTRO' ? '' : _bankCode,
      currency: source.currency,
      amountMinor: _amountMinor!,
      motive: _motive.trim(),
      idempotencyKey: _idempotencyKey,
    );
    final outcome = await context.push<CciOutcome>(
      '/transfers/confirm',
      extra: TransferFlowArgs(draft: draft, source: source),
    );
    if (!mounted) return;
    // La confirmación valida el titular contra la CCE: si volvió con un
    // resultado CCI, el formulario refleja el banner correspondiente.
    if (outcome != null &&
        (outcome.state == CciState.notFound ||
            outcome.state == CciState.unavailable)) {
      setState(() => _cciState = outcome.state);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Transferencia Interbancaria')),
      body: FutureBuilder<List<Account>>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingView(message: 'Cargando cuentas...');
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            return Padding(
              padding: const EdgeInsets.all(16),
              child: ErrorView(
                message:
                    error is ApiException
                        ? transferUserMessage(error)
                        : 'Ocurrió un error al consultar el servidor.',
                onRetry: _retryLoad,
              ),
            );
          }
          final accounts = snapshot.data ?? const <Account>[];
          if (accounts.isEmpty) {
            return Padding(
              padding: const EdgeInsets.all(16),
              child: EmptyView(
                message: 'Aún no tienes cuentas para transferir.',
                actionLabel: 'Volver',
                onAction: () => context.pop(),
              ),
            );
          }
          _originId ??= accounts.first.id;
          final source = accounts.firstWhere(
            (a) => a.id == _originId,
            orElse: () => accounts.first,
          );
          _originId = source.id;
          // Cache para el reintento del banner CCE (misma clave del intento).
          _lastAccounts = accounts;
          return Column(
            children: [
              Expanded(
                child: ListView(
                  key: const Key('interbank-form-list'),
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
                  children: [
                    _originCard(accounts),
                    const SizedBox(height: 16),
                    _destinationCard(),
                    const SizedBox(height: 16),
                    _amountCard(),
                  ],
                ),
              ),
              SafeArea(
                child: Container(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
                  color: AppColors.surface,
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      SizedBox(
                        width: double.infinity,
                        child: FilledButton.icon(
                          key: const Key('interbank-continue'),
                          onPressed:
                              _valid ? () => _continue(accounts) : null,
                          icon: const Icon(Icons.chevron_right, size: 18),
                          label: const Text('Continuar a confirmación'),
                          style: FilledButton.styleFrom(
                            minimumSize: const Size(88, 52),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(
                                AppRadii.md,
                              ),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(height: 8),
                      Row(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          const Icon(
                            Icons.lock_outline,
                            size: 14,
                            color: AppColors.outline,
                          ),
                          const SizedBox(width: 6),
                          Text(
                            'Operación encriptada TLS 1.3',
                            style: AppTypography.bodyMd.copyWith(
                              fontSize: 12,
                              color: AppColors.outline,
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }

  Widget _originCard(List<Account> accounts) {
    return AppCard(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                'DESDE MI CUENTA',
                style: AppTypography.labelMd.copyWith(
                  fontSize: 12,
                  color: AppColors.secondary,
                ),
              ),
              Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 8,
                  vertical: 4,
                ),
                decoration: BoxDecoration(
                  color: AppColors.successContainer,
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text(
                  'Principal',
                  style: AppTypography.labelSm.copyWith(
                    fontSize: 11,
                    color: AppColors.onSuccessContainer,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          AccountOptionList(
            accounts: accounts,
            selectedId: _originId,
            keyPrefix: 'interbank-origin',
            onSelected: (a) => setState(() => _originId = a.id),
          ),
        ],
      ),
    );
  }

  Widget _destinationCard() {
    return AppCard(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                'DESTINO INTERBANCARIO CCE',
                style: AppTypography.labelMd.copyWith(
                  fontSize: 12,
                  color: AppColors.secondary,
                ),
              ),
              Text(
                'Inmediata 24/7',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 11,
                  color: AppColors.outline,
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Text(
            'Entidad Financiera Destino',
            style: AppTypography.labelMd.copyWith(
              fontSize: 12,
              color: AppColors.onSurface,
            ),
          ),
          const SizedBox(height: 8),
          DropdownButtonFormField<String>(
            key: const Key('bank-picker'),
            initialValue: _bankCode,
            items: [
              for (final b in _banks)
                DropdownMenuItem(
                  value: b,
                  child: Text(_bankLabel(b)),
                ),
            ],
            onChanged: (v) => setState(() => _bankCode = v ?? 'BCP'),
            decoration: InputDecoration(
              filled: true,
              fillColor: AppColors.surfaceContainerLow,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: BorderSide.none,
              ),
            ),
          ),
          const SizedBox(height: 12),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                'Código Interbancario (CCI)',
                style: AppTypography.labelMd.copyWith(
                  fontSize: 12,
                  color: AppColors.onSurface,
                ),
              ),
              Text(
                key: const Key('cci-counter'),
                '${_digits.length} / 20',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 11,
                  color: AppColors.outline,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          TextField(
            key: const Key('cci-field'),
            controller: _cciController,
            keyboardType: TextInputType.number,
            inputFormatters: [
              FilteringTextInputFormatter.allow(RegExp(r'[0-9\s\-]')),
              LengthLimitingTextInputFormatter(26),
            ],
            onChanged: _onCciChanged,
            style: AppTypography.bodyLg.copyWith(
              fontWeight: FontWeight.w500,
              color: AppColors.onSurface,
            ),
            decoration: InputDecoration(
              hintText: '002-191-002847291044-58',
              errorText: _cciError,
              filled: true,
              fillColor: AppColors.surfaceContainerLow,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: BorderSide.none,
              ),
            ),
          ),
          const SizedBox(height: 12),
          CciStatusBanner(
            state: _cciState,
            holderMasked: _holderMasked,
            accountLabel:
                _cciState == CciState.verified
                    ? 'Cuenta ${_bankLabel(_bankCode)}'
                    : '',
            onRetry:
                _cciState == CciState.unavailable
                    ? () {
                      final accounts = snapshotAccounts();
                      if (accounts != null) _continue(accounts);
                    }
                    : null,
          ),
        ],
      ),
    );
  }

  /// Cuentas ya cargadas (el reintento del banner reutiliza el formulario
  /// y la MISMA clave del intento en curso).
  List<Account>? snapshotAccounts() {
    // Se resuelve en el build via FutureBuilder; el banner solo aparece
    // tras volver de la confirmación, con datos ya cargados.
    return _lastAccounts;
  }

  List<Account>? _lastAccounts;

  Widget _amountCard() {
    final minor = _amountMinor;
    return AppCard(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                'MONTO A TRANSFERIR',
                style: AppTypography.labelMd.copyWith(
                  fontSize: 12,
                  color: AppColors.secondary,
                ),
              ),
              Text(
                'Límite: S/ 30,000',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 11,
                  color: AppColors.outline,
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          TextField(
            key: const Key('interbank-amount'),
            controller: _amountController,
            keyboardType: const TextInputType.numberWithOptions(decimal: true),
            inputFormatters: [
              FilteringTextInputFormatter.allow(RegExp(r'[0-9.]')),
            ],
            onChanged: _onAmountChanged,
            decoration: InputDecoration(
              prefixText: 'S/ ',
              hintText: '350.00',
              errorText: _amountError,
              filled: true,
              fillColor: AppColors.surfaceContainerLow,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: BorderSide.none,
              ),
            ),
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              for (var i = 0; i < _quickChips.length; i++) ...[
                Expanded(
                  child: InkWell(
                    key: Key('interbank-chip-${_quickChips[i]}'),
                    onTap: () {
                      _amountController.text = '${_quickChips[i]}.00';
                      _onAmountChanged('${_quickChips[i]}.00');
                    },
                    borderRadius: BorderRadius.circular(AppRadii.def),
                    child: Container(
                      height: 32,
                      alignment: Alignment.center,
                      decoration: BoxDecoration(
                        color: AppColors.surfaceContainer,
                        borderRadius: BorderRadius.circular(AppRadii.def),
                      ),
                      child: Text(
                        'S/ ${_quickChips[i]}',
                        style: AppTypography.labelMd.copyWith(
                          fontSize: 12,
                          color: AppColors.onSurface,
                        ),
                      ),
                    ),
                  ),
                ),
                if (i < _quickChips.length - 1)
                  const SizedBox(width: 8),
              ],
            ],
          ),
          const SizedBox(height: 12),
          Text(
            'Motivo (opcional)',
            style: AppTypography.labelMd.copyWith(
              fontSize: 12,
              color: AppColors.onSurface,
            ),
          ),
          const SizedBox(height: 8),
          TextField(
            key: const Key('interbank-motive'),
            controller: _motiveController,
            maxLength: 40,
            onChanged: (v) => setState(() => _motive = v),
            decoration: InputDecoration(
              hintText: 'Ej. Alquiler de departamento...',
              filled: true,
              fillColor: AppColors.surfaceContainerLow,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: BorderSide.none,
              ),
            ),
          ),
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLow,
              borderRadius: BorderRadius.circular(AppRadii.md),
            ),
            child: Column(
              children: [
                _moneyRow(
                  'Subtotal transferencia',
                  minor == null || minor <= 0
                      ? '—'
                      : formatMoneyMinor(minor, 'PEN'),
                ),
                _moneyRow('Comisión CCE', 'La calcula el banco'),
                const Divider(color: AppColors.divider),
                _moneyRow(
                  'Total a debitar',
                  minor == null || minor <= 0
                      ? '—'
                      : '${formatMoneyMinor(minor, 'PEN')} + comisión',
                  bold: true,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _moneyRow(String k, String v, {bool bold = false}) {
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
              fontSize: bold ? 16 : 12,
              fontWeight: bold ? FontWeight.bold : FontWeight.w500,
              color: AppColors.onSurface,
            ),
          ),
        ],
      ),
    );
  }

  String _bankLabel(String code) {
    switch (code) {
      case 'BCP':
        return 'BCP - Banco de Crédito';
      case 'INTERBANK':
        return 'Interbank';
      case 'BBVA':
        return 'BBVA Perú';
      case 'SCOTIABANK':
        return 'Scotiabank';
      default:
        return 'Otro banco';
    }
  }
}
