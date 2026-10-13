import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../../accounts/models/account.dart';
import '../../beneficiaries/models/beneficiary.dart';
import '../data/transfers_service.dart';
import '../models/resolve_target.dart';
import '../models/transfer.dart';
import 'widgets/account_option_list.dart';

/// Via de destino a tercero (E2-T29): agenda, DNI o celular.
///
/// DNI/celular exigen lookup previo (`resolve-target`) y solo con el
/// titular enmascarado resuelto se habilita continuar; la transferencia
/// posterior usa el `target_account_id` devuelto (el POST no cambia).
enum ThirdPartyDestinationMode {
  /// Beneficiario de la agenda (`GET /beneficiaries`).
  beneficiary,

  /// DNI con lookup previo (`target_document`).
  dni,

  /// Celular con lookup previo (`target_phone`).
  phone,
}

/// Seleccion de origen y destino (mockups `Transferencia Entre Mis Cuentas`
/// y `Enviar - Selección Beneficiario`, `pantallas.fig`).
///
/// - Origen: cuentas propias (`GET /accounts`, 4 estados).
/// - Destino propio: otra cuenta propia (se excluye el origen).
/// - Destino tercero: beneficiario de la agenda (`GET /beneficiaries`,
///   datos enmascarados; 4 estados) o DNI/celular con lookup previo
///   (`POST /transfers/third-party/resolve-target`, E2-T29: muestra el
///   titular enmascarado antes de confirmar y continua con el
///   `target_account_id` resuelto).
///
/// Al continuar navega a `/transfers/amount` con [TransferFlowArgs] (la
/// `Idempotency-Key` del intento se genera aqui una sola vez; el lookup
/// previo es lectura y NO usa `Idempotency-Key`).
class DestinationPage extends StatefulWidget {
  const DestinationPage({super.key, required this.kind, this.service});

  final TransferKind kind;
  final TransfersServiceBase? service;

  @override
  State<DestinationPage> createState() => _DestinationPageState();
}

class _DestinationPageState extends State<DestinationPage> {
  Future<_DestinationData>? _future;
  String? _sourceId;
  String? _targetId;
  String? _beneficiaryId;

  ThirdPartyDestinationMode _mode = ThirdPartyDestinationMode.beneficiary;
  final _dniController = TextEditingController();
  final _phoneController = TextEditingController();
  ResolvedTarget? _resolved;
  bool _resolving = false;
  String? _lookupError;
  String? _lookupFieldError;

  @override
  void dispose() {
    _dniController.dispose();
    _phoneController.dispose();
    super.dispose();
  }

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<_DestinationData> _load() async {
    final service = widget.service;
    if (service == null) {
      throw ApiException(
        code: 'UNKNOWN',
        message: 'Servicio de transferencias no configurado.',
      );
    }
    final accounts = await service.accounts();
    final beneficiaries = widget.kind == TransferKind.thirdParty
        ? await service.beneficiaries()
        : const <Beneficiary>[];
    return _DestinationData(accounts: accounts, beneficiaries: beneficiaries);
  }

  void _retry() {
    setState(() {
      _sourceId = null;
      _targetId = null;
      _beneficiaryId = null;
      _mode = ThirdPartyDestinationMode.beneficiary;
      _dniController.clear();
      _phoneController.clear();
      _resolved = null;
      _resolving = false;
      _lookupError = null;
      _lookupFieldError = null;
      _future = _load();
    });
  }

  /// Alta de beneficiario desde el flujo de transferencia (fix alta
  /// múltiple): abre `/beneficiaries/new` y, si el formulario devuelve
  /// `true` (alta guardada), recarga la agenda con [_load] preservando
  /// origen/modo/selección para que el nuevo beneficiario aparezca de
  /// inmediato sin perder lo ya elegido.
  Future<void> _openNewBeneficiary() async {
    final created = await context.push<bool>('/beneficiaries/new');
    if (!mounted) return;
    if (created == true) {
      setState(() {
        _future = _load();
      });
    }
  }

  /// Cambia la via de destino limpiando campos, titular resuelto y
  /// errores (seleccion mutuamente excluyente, E2-T29).
  void _setMode(ThirdPartyDestinationMode mode) {
    if (_mode == mode) return;
    setState(() {
      _mode = mode;
      _beneficiaryId = null;
      _dniController.clear();
      _phoneController.clear();
      _resolved = null;
      _resolving = false;
      _lookupError = null;
      _lookupFieldError = null;
    });
  }

  /// Al editar el DNI/celular tras un lookup, el titular resuelto queda
  /// stale: se limpia el panel y se deshabilita continuar hasta volver
  /// a buscar (E2-T29 fix: evita transferir a un destino ya cambiado).
  void _onLookupFieldChanged(String _) {
    if (_resolved == null &&
        _lookupError == null &&
        _lookupFieldError == null) {
      return;
    }
    setState(() {
      _resolved = null;
      _lookupError = null;
      _lookupFieldError = null;
    });
  }

  /// Lookup previo por DNI/celular: valida formato local, consulta
  /// `resolve-target` y muestra el titular enmascarado (sin PII en
  /// logs: el documento/celular solo viaja en el request).
  Future<void> _lookup() async {
    if (_resolving) return;
    final service = widget.service;
    if (service == null) {
      setState(() {
        _lookupError = 'Servicio de transferencias no configurado.';
      });
      return;
    }
    final isDni = _mode == ThirdPartyDestinationMode.dni;
    final raw = isDni ? _dniController.text : _phoneController.text;
    final fieldError = isDni ? dniFormatError(raw) : phoneFormatError(raw);
    if (fieldError != null) {
      setState(() {
        _lookupFieldError = fieldError;
        _lookupError = null;
        _resolved = null;
      });
      return;
    }
    setState(() {
      _resolving = true;
      _lookupFieldError = null;
      _lookupError = null;
      _resolved = null;
    });
    try {
      final resolved = isDni
          ? await service.resolveTarget(
              documentType: 'DNI',
              documentNumber: normalizeLookupDigits(raw),
            )
          : await service.resolveTarget(phone: normalizeLookupDigits(raw));
      if (!mounted) return;
      setState(() {
        _resolving = false;
        _resolved = resolved;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _resolving = false;
        if (e.code == 'TARGET_NOT_FOUND') {
          _lookupError = kTargetNotFoundMessage;
        } else if (e.code == 'TARGET_AMBIGUOUS') {
          _lookupError = kTargetAmbiguousMessage;
        } else {
          _lookupError = transferUserMessage(e);
        }
      });
    }
  }

  bool get _canContinue {
    if (_sourceId == null) return false;
    if (widget.kind == TransferKind.own) {
      return _targetId != null && _targetId != _sourceId;
    }
    // Tercero: agenda exige beneficiario; DNI/celular exigen el
    // titular resuelto por el lookup previo (E2-T29 CA-02).
    if (_mode != ThirdPartyDestinationMode.beneficiary) {
      return _resolved != null && !_resolving;
    }
    return _beneficiaryId != null;
  }

  void _continue(_DestinationData data) {
    final source = data.accounts.firstWhere((a) => a.id == _sourceId);
    if (widget.kind == TransferKind.own) {
      final target = data.accounts.firstWhere((a) => a.id == _targetId);
      final draft = TransferDraft(
        kind: TransferKind.own,
        sourceAccountId: source.id,
        targetAccountId: target.id,
        destinationMasked: target.accountNumberMasked,
        beneficiaryAlias: 'Cuenta CuyCash ${accountTypeLabel(target.type)}',
        currency: source.currency,
        amountMinor: 0,
      );
      context.push(
        '/transfers/amount',
        extra: TransferFlowArgs(draft: draft, source: source, target: target),
      );
      return;
    }
    // Destino por DNI/celular (E2-T29): la transferencia usa el
    // `target_account_id` resuelto por el lookup (no DNI/celular); el
    // alias visible es el titular enmascarado del servidor.
    if (_mode != ThirdPartyDestinationMode.beneficiary) {
      final resolved = _resolved;
      if (resolved == null) return;
      final draft = TransferDraft(
        kind: TransferKind.thirdParty,
        sourceAccountId: source.id,
        targetAccountId: resolved.targetAccountId,
        beneficiaryAlias: resolved.holderNameMasked,
        destinationMasked:
            '${resolved.accountNumberMasked} · ${currencyAccountLabel(resolved.currency)}',
        currency: resolved.currency,
        amountMinor: 0,
      );
      context.push(
        '/transfers/amount',
        extra: TransferFlowArgs(draft: draft, source: source),
      );
      return;
    }
    final beneficiary = data.beneficiaries.firstWhere(
      (b) => b.id == _beneficiaryId,
    );
    // Destino por agenda (E2-T30): el backend resuelve server-side la
    // cuenta real del beneficiario a partir de `beneficiary_id` (el
    // borrador NO lleva `targetAccountId`); el `beneficiary_id` ademas
    // habilita la exencion por tope (E2-T14). Si el backend responde
    // `NOT_FOUND`, la confirmacion muestra "cuenta destino invalida".
    final draft = TransferDraft(
      kind: TransferKind.thirdParty,
      sourceAccountId: source.id,
      beneficiaryId: beneficiary.id,
      beneficiaryAlias: beneficiary.alias,
      destinationMasked:
          '${beneficiary.accountMasked} · ${currencyAccountLabel(beneficiary.currency)}',
      currency: beneficiary.currency,
      amountMinor: 0,
    );
    context.push(
      '/transfers/amount',
      extra: TransferFlowArgs(draft: draft, source: source),
    );
  }

  @override
  Widget build(BuildContext context) {
    final isOwn = widget.kind == TransferKind.own;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        title: Text(isOwn ? 'Entre mis cuentas' : 'Elegir destinatario'),
        actions: [
          // Fix alta múltiple (Nivel 1): acción "Nuevo" siempre visible en
          // el flujo a tercero cuando el modo es agenda/beneficiario, para
          // que el alta no dependa del estado vacío de la lista.
          if (!isOwn && _mode == ThirdPartyDestinationMode.beneficiary)
            TextButton.icon(
              key: const Key('destination-new-beneficiary'),
              onPressed: _openNewBeneficiary,
              icon: const Icon(Icons.add, size: 20),
              label: const Text('Nuevo'),
            ),
        ],
      ),
      body: FutureBuilder<_DestinationData>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingView(message: 'Cargando cuentas...');
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            final message = error is ApiException
                ? transferUserMessage(error)
                : 'Ocurrió un error al consultar el servidor. '
                      'Revisa tu conexión a internet o intenta de nuevo.';
            return Padding(
              padding: const EdgeInsets.all(16),
              child: ErrorView(message: message, onRetry: _retry),
            );
          }
          final data = snapshot.data!;
          if (data.accounts.isEmpty) {
            return Padding(
              padding: const EdgeInsets.all(16),
              child: EmptyView(
                message:
                    'Aún no tienes cuentas para transferir.\n'
                    'Abre tu primera cuenta para empezar.',
                actionLabel: 'Ir al inicio',
                // m7: el copy promete el inicio: navega a `/home` (antes
                // hacia `pop()`, que solo volvia atras).
                onAction: () => context.go('/home'),
              ),
            );
          }
          _sourceId ??= data.accounts.first.id;
          final source = data.accounts.firstWhere(
            (a) => a.id == _sourceId,
            orElse: () => data.accounts.first,
          );
          _sourceId = source.id;
          return Column(
            children: [
              Expanded(
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
                  children: [
                    _SectionLabel(isOwn ? 'DESDE MI CUENTA' : 'CUENTA ORIGEN'),
                    const SizedBox(height: 8),
                    AccountOptionList(
                      accounts: data.accounts,
                      selectedId: _sourceId,
                      keyPrefix: 'transfer-origin',
                      onSelected: (a) => setState(() {
                        _sourceId = a.id;
                        if (_targetId == a.id) _targetId = null;
                      }),
                    ),
                    const SizedBox(height: 16),
                    _SectionLabel(
                      isOwn ? 'A MI CUENTA' : 'DESTINO DEL TERCERO',
                    ),
                    const SizedBox(height: 8),
                    if (isOwn)
                      AccountOptionList(
                        accounts: data.accounts,
                        selectedId: _targetId,
                        excludeId: _sourceId,
                        keyPrefix: 'transfer-dest',
                        onSelected: (a) => setState(() => _targetId = a.id),
                      )
                    else ...[
                      _ModeTabs(mode: _mode, onChanged: _setMode),
                      const SizedBox(height: 12),
                      if (_mode == ThirdPartyDestinationMode.beneficiary)
                        _BeneficiaryOptions(
                          beneficiaries: data.beneficiaries,
                          selectedId: _beneficiaryId,
                          onSelected: (b) =>
                              setState(() => _beneficiaryId = b.id),
                          onNew: _openNewBeneficiary,
                        )
                      else
                        _LookupPanel(
                          isDni: _mode == ThirdPartyDestinationMode.dni,
                          dniController: _dniController,
                          phoneController: _phoneController,
                          fieldError: _lookupFieldError,
                          lookupError: _lookupError,
                          resolving: _resolving,
                          resolved: _resolved,
                          onLookup: _lookup,
                          onChanged: _onLookupFieldChanged,
                        ),
                    ],
                  ],
                ),
              ),
              SafeArea(
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
                  child: SizedBox(
                    width: double.infinity,
                    child: FilledButton(
                      key: const Key('destination-continue'),
                      onPressed: _canContinue ? () => _continue(data) : null,
                      style: FilledButton.styleFrom(
                        minimumSize: const Size(88, 52),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(AppRadii.md),
                        ),
                      ),
                      child: const Text('Continuar'),
                    ),
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _DestinationData {
  const _DestinationData({required this.accounts, required this.beneficiaries});

  final List<Account> accounts;
  final List<Beneficiary> beneficiaries;
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Text(
      text,
      style: AppTypography.labelMd.copyWith(
        fontSize: 12,
        color: AppColors.secondary,
      ),
    );
  }
}

class _BeneficiaryOptions extends StatelessWidget {
  const _BeneficiaryOptions({
    required this.beneficiaries,
    required this.selectedId,
    required this.onSelected,
    required this.onNew,
  });

  final List<Beneficiary> beneficiaries;
  final String? selectedId;
  final ValueChanged<Beneficiary> onSelected;
  final VoidCallback onNew;

  @override
  Widget build(BuildContext context) {
    if (beneficiaries.isEmpty) {
      return EmptyView(
        message:
            'Aún no tienes beneficiarios.\n'
            'Guarda tus cuentas frecuentes para transferir en un toque.',
        actionLabel: 'Agregar beneficiario',
        onAction: onNew,
      );
    }
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (final b in beneficiaries) ...[
          AppCard(
            key: Key('transfer-beneficiary-card-${b.id}'),
            padding: const EdgeInsets.all(14),
            onTap: () => onSelected(b),
            child: Row(
              children: [
                Container(
                  width: 44,
                  height: 44,
                  alignment: Alignment.center,
                  decoration: const BoxDecoration(
                    color: AppColors.surfaceContainerHigh,
                    shape: BoxShape.circle,
                  ),
                  child: Text(
                    b.initials,
                    style: AppTypography.labelMd.copyWith(
                      fontWeight: FontWeight.bold,
                      color: AppColors.onSurfaceVariant,
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        b.alias,
                        style: AppTypography.bodyLg.copyWith(
                          fontWeight: FontWeight.w600,
                          color: AppColors.onSurface,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        b.holderName.isEmpty ? '••••' : b.holderName,
                        style: AppTypography.bodyMd.copyWith(
                          fontSize: 13,
                          color: AppColors.onSurfaceVariant,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        '${b.accountMasked} · ${currencyAccountLabel(b.currency)}',
                        style: AppTypography.bodyMd.copyWith(
                          fontSize: 12,
                          color: AppColors.outline,
                        ),
                      ),
                    ],
                  ),
                ),
                Icon(
                  selectedId == b.id
                      ? Icons.radio_button_checked
                      : Icons.radio_button_off,
                  key: Key('transfer-beneficiary-${b.id}'),
                  color: selectedId == b.id
                      ? AppColors.primary
                      : AppColors.outline,
                ),
              ],
            ),
          ),
          const SizedBox(height: 8),
        ],
        // Fix alta múltiple (Nivel 1): la opción "Agregar beneficiario"
        // SIEMPRE visible, también con lista no vacía (antes solo existía
        // en el estado vacío y con ≥1 beneficiario no había acción de alta).
        SizedBox(
          width: double.infinity,
          child: OutlinedButton.icon(
            key: const Key('transfer-beneficiary-add'),
            onPressed: onNew,
            icon: const Icon(Icons.add, size: 20),
            label: const Text('Agregar beneficiario'),
            style: OutlinedButton.styleFrom(
              minimumSize: const Size(88, 52),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

/// Selector de via de destino a tercero (E2-T29): agenda, DNI o celular.
///
/// Mutuamente excluyente; al cambiar se limpian campos, titular
/// resuelto y errores ([_DestinationPageState._setMode]).
class _ModeTabs extends StatelessWidget {
  const _ModeTabs({required this.mode, required this.onChanged});

  final ThirdPartyDestinationMode mode;
  final ValueChanged<ThirdPartyDestinationMode> onChanged;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(4),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerHigh,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          _ModeTab(
            key: const Key('dest-mode-beneficiary'),
            label: 'Beneficiario',
            selected: mode == ThirdPartyDestinationMode.beneficiary,
            onTap: () => onChanged(ThirdPartyDestinationMode.beneficiary),
          ),
          _ModeTab(
            key: const Key('dest-mode-dni'),
            label: 'DNI',
            selected: mode == ThirdPartyDestinationMode.dni,
            onTap: () => onChanged(ThirdPartyDestinationMode.dni),
          ),
          _ModeTab(
            key: const Key('dest-mode-phone'),
            label: 'Celular',
            selected: mode == ThirdPartyDestinationMode.phone,
            onTap: () => onChanged(ThirdPartyDestinationMode.phone),
          ),
        ],
      ),
    );
  }
}

class _ModeTab extends StatelessWidget {
  const _ModeTab({
    super.key,
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: GestureDetector(
        onTap: onTap,
        child: Container(
          height: 36,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: selected
                ? AppColors.surfaceContainerLowest
                : Colors.transparent,
            borderRadius: BorderRadius.circular(8),
          ),
          child: Text(
            label,
            style: AppTypography.labelMd.copyWith(
              fontSize: 12,
              fontWeight: selected ? FontWeight.w600 : FontWeight.w500,
              color: selected ? AppColors.primary : AppColors.onSurfaceVariant,
            ),
          ),
        ),
      ),
    );
  }
}

/// Panel de lookup previo por DNI/celular (E2-T29).
///
/// Cubre los 4 estados: inicial (ayuda de formato), carga
/// (`Consultando...`), titular resuelto (mascaras del servidor) y
/// error (neutro `TARGET_NOT_FOUND` o accionable `TARGET_AMBIGUOUS`).
/// El documento/celular en claro nunca se exhibe mas alla del campo.
class _LookupPanel extends StatelessWidget {
  const _LookupPanel({
    required this.isDni,
    required this.dniController,
    required this.phoneController,
    required this.fieldError,
    required this.lookupError,
    required this.resolving,
    required this.resolved,
    required this.onLookup,
    required this.onChanged,
  });

  final bool isDni;
  final TextEditingController dniController;
  final TextEditingController phoneController;
  final String? fieldError;
  final String? lookupError;
  final bool resolving;
  final ResolvedTarget? resolved;
  final VoidCallback onLookup;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) {
    final resolvedTarget = resolved;
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          isDni ? 'DNI del destinatario' : 'Celular del destinatario',
          style: AppTypography.labelMd.copyWith(
            fontSize: 12,
            fontWeight: FontWeight.w600,
            color: AppColors.secondary,
          ),
        ),
        const SizedBox(height: 8),
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: TextField(
                key: Key(isDni ? 'dest-dni-input' : 'dest-phone-input'),
                controller: isDni ? dniController : phoneController,
                keyboardType: TextInputType.number,
                textInputAction: TextInputAction.search,
                onChanged: onChanged,
                onSubmitted: (_) => onLookup(),
                decoration: InputDecoration(
                  hintText: isDni ? '42891044' : '987654321',
                  helperText: isDni
                      ? 'DNI de 8 o 9 dígitos'
                      : 'Celular de 9 dígitos (empieza con 9)',
                  errorText: fieldError,
                  filled: true,
                  fillColor: AppColors.surfaceContainerLowest,
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            SizedBox(
              height: 56,
              child: FilledButton(
                key: const Key('dest-lookup-button'),
                onPressed: resolving ? null : onLookup,
                child: resolving
                    ? const SizedBox.square(
                        key: Key('dest-lookup-loading'),
                        dimension: 20,
                        child: CircularProgressIndicator(strokeWidth: 2.5),
                      )
                    : const Text('Buscar'),
              ),
            ),
          ],
        ),
        if (lookupError != null) ...[
          const SizedBox(height: 8),
          Text(
            lookupError!,
            key: const Key('dest-lookup-error'),
            style: AppTypography.bodyMd.copyWith(
              fontSize: 13,
              color: AppColors.error,
            ),
          ),
        ],
        if (resolvedTarget != null) ...[
          const SizedBox(height: 12),
          AppCard(
            key: const Key('dest-resolved-card'),
            padding: const EdgeInsets.all(14),
            child: Row(
              children: [
                Container(
                  width: 44,
                  height: 44,
                  alignment: Alignment.center,
                  decoration: const BoxDecoration(
                    color: AppColors.surfaceContainerHigh,
                    shape: BoxShape.circle,
                  ),
                  child: const Icon(
                    Icons.verified_user_outlined,
                    size: 22,
                    color: AppColors.primary,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        resolvedTarget.holderNameMasked,
                        style: AppTypography.bodyLg.copyWith(
                          fontWeight: FontWeight.w600,
                          color: AppColors.onSurface,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        '${resolvedTarget.accountNumberMasked} · '
                        '${currencyAccountLabel(resolvedTarget.currency)}',
                        style: AppTypography.bodyMd.copyWith(
                          fontSize: 12,
                          color: AppColors.outline,
                        ),
                      ),
                    ],
                  ),
                ),
                const Icon(
                  Icons.check_circle,
                  key: Key('dest-resolved-check'),
                  color: AppColors.primary,
                ),
              ],
            ),
          ),
        ],
      ],
    );
  }
}
