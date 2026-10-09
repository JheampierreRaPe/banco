import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_text_field.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/beneficiaries_service.dart';
import '../models/beneficiary.dart';
import '../utils/account_number.dart';
import 'delete_beneficiary_dialog.dart';

/// Tipo de documento del mockup (selector DNI/RUC + campo SUNAT/RENIEC).
enum BeneficiaryDocType { dni, ruc }

/// Formulario de alta/edicion de beneficiario (HU07 CA-03/CA-04, E2-T15;
/// mockups `Alta Edición Beneficiario` + `Alta 2/3/4/5` en `pantallas.fig`).
///
/// - Alta (`beneficiaryId == null`) o edicion (con [initial] o carga por id).
/// - Selector DNI (8 digitos) / RUC (11 digitos) con boton `Validar` y banner
///   de formato (mockups `Alta` y `Alta 4 - RUC SUNAT`). La validacion de
///   formato es solo longitud/digitos (UI); el backend decide las reglas.
/// - Error de cuenta/CCI bajo el campo (mockup `Alta 3 - Error CCI`): el
///   cliente valida el formato (14 digitos con DV modulo 11 o 20 CCI,
///   E2-T29); el backend revalida y decide (`VALIDATION_ERROR`).
/// - Duplicado con banner naranja + `Ver existente`/`Ignorar` (mockup
///   `Alta 2 - Duplicado`, codigo `DUPLICATE_BENEFICIARY` visible).
/// - Tope pre-aprobado RETIRADO (decision del dueno 8-10-2026-2.txt D1,
///   E2-T33): el formulario no ofrece ni envia tope; la unica regla es el
///   umbral biometrico general (S/ 1,000, lo decide el backend).
/// - Eliminar (solo edicion) con [DeleteBeneficiaryDialog] (mockup
///   `Alta 5 - Eliminar`).
/// - El CCI/cuenta en claro solo viaja en el request; jamas se muestra
///   completo en lista ni se loguea.
class BeneficiaryFormPage extends StatefulWidget {
  const BeneficiaryFormPage({
    super.key,
    this.service,
    this.beneficiaryId,
    this.initial,
  });

  final BeneficiariesServiceBase? service;

  /// Id para edicion por deep-link (sin [initial] se carga de la lista).
  final String? beneficiaryId;

  /// Beneficiario a editar (viene de la lista via `extra`).
  final Beneficiary? initial;

  bool get isEditing => beneficiaryId != null || initial != null;

  @override
  State<BeneficiaryFormPage> createState() => _BeneficiaryFormPageState();
}

class _BeneficiaryFormPageState extends State<BeneficiaryFormPage> {
  final _aliasController = TextEditingController();
  final _docController = TextEditingController();
  final _accountController = TextEditingController();

  BeneficiaryDocType _docType = BeneficiaryDocType.dni;
  String _bankCode = 'BCP';
  String _currency = 'PEN';

  bool _docVerified = false;
  bool _showDuplicate = false;
  String? _accountError;
  String? _aliasError;
  String? _docError;
  bool _saving = false;
  bool _initialized = false;
  Future<Beneficiary?>? _initialFuture;

  static const _banks = [
    ('BCP', 'BCP - Banco de Crédito'),
    ('INTERBANK', 'Interbank'),
    ('BBVA', 'BBVA Perú'),
    ('SCOTIABANK', 'Scotiabank'),
    ('OTRO', 'Otro banco'),
  ];

  @override
  void initState() {
    super.initState();
    final initial = widget.initial;
    if (initial != null) {
      _applyInitial(initial);
    } else if (widget.beneficiaryId != null) {
      _initialFuture = _loadInitial(widget.beneficiaryId!);
    } else {
      _initialized = true;
    }
  }

  @override
  void dispose() {
    _aliasController.dispose();
    _docController.dispose();
    _accountController.dispose();
    super.dispose();
  }

  Future<Beneficiary?> _loadInitial(String id) async {
    final service = widget.service;
    if (service == null) {
      throw ApiException(
        code: 'UNKNOWN',
        message: 'Servicio de beneficiarios no configurado.',
      );
    }
    final items = await service.list();
    for (final b in items) {
      if (b.id == id) {
        _applyInitial(b);
        return b;
      }
    }
    throw ApiException(code: 'NOT_FOUND', message: 'No encontramos ese beneficiario.');
  }

  void _applyInitial(Beneficiary b) {
    _aliasController.text = b.alias;
    _bankCode = b.bankCode.isEmpty ? 'BCP' : b.bankCode.toUpperCase();
    if (!_banks.any((e) => e.$1 == _bankCode)) _bankCode = 'BCP';
    _currency = b.currency.toUpperCase() == 'USD' ? 'USD' : 'PEN';
    _initialized = true;
  }

  String _digits(String value) => value.replaceAll(RegExp(r'\D'), '');

  void _validateDoc() {
    final digits = _digits(_docController.text);
    final expected = _docType == BeneficiaryDocType.dni ? 8 : 11;
    final kind = _docType == BeneficiaryDocType.dni ? 'DNI' : 'RUC';
    setState(() {
      if (digits.length != expected) {
        _docError = 'Ingresa $expected dígitos para $kind.';
        _docVerified = false;
      } else {
        _docError = null;
        _docVerified = true;
      }
    });
  }

  bool _validateLocal() {
    var ok = true;
    if (_aliasController.text.trim().isEmpty) {
      _aliasError = 'Ingresa un alias para identificarlo.';
      ok = false;
    } else {
      _aliasError = null;
    }
    final docDigits = _digits(_docController.text);
    final expected = _docType == BeneficiaryDocType.dni ? 8 : 11;
    if (docDigits.length != expected) {
      _docError =
          'Ingresa $expected dígitos para ${_docType == BeneficiaryDocType.dni ? 'DNI' : 'RUC'}.';
      _docVerified = false;
      ok = false;
    }
    final accountError = accountFormatError(_accountController.text);
    if (accountError != null) {
      _accountError = accountError;
      ok = false;
    } else {
      _accountError = null;
    }
    setState(() {});
    return ok;
  }

  Future<void> _save() async {
    if (_saving) return;
    if (!_validateLocal()) return;
    final service = widget.service;
    if (service == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Servicio de beneficiarios no configurado.'),
        ),
      );
      return;
    }
    setState(() {
      _saving = true;
      _showDuplicate = false;
    });
    final input = BeneficiaryInput(
      alias: _aliasController.text.trim(),
      accountOrCci: normalizeAccountOrCci(_accountController.text),
      bankCode: _bankCode == 'OTRO' ? null : _bankCode,
      holderDoc: _digits(_docController.text),
      currency: _currency,
    );
    try {
      final editing = widget.isEditing;
      if (editing) {
        final id = widget.initial?.id ?? widget.beneficiaryId ?? '';
        await service.update(id, input);
      } else {
        await service.create(input);
      }
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            editing
                ? 'Beneficiario actualizado.'
                : 'Beneficiario guardado.',
          ),
        ),
      );
      _closeWithResult(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _saving = false);
      if (e.code == 'DUPLICATE_BENEFICIARY') {
        setState(() => _showDuplicate = true);
        return;
      }
      if (e.code == 'VALIDATION_ERROR') {
        setState(() {
          _accountError =
              'Dígito verificador de cuenta incorrecto. '
              'Verifica los 14 dígitos (cuenta) o 20 (CCI).';
        });
        return;
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(_friendlyFormError(e.code))),
      );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  /// Vuelve a la agenda con resultado; si no hay pila (deep-link directo),
  /// navega a `/beneficiaries` en vez de romper.
  void _closeWithResult(bool result) {
    if (context.canPop()) {
      context.pop(result);
    } else {
      context.go('/beneficiaries');
    }
  }

  Future<void> _delete(Beneficiary target) async {
    final service = widget.service;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => DeleteBeneficiaryDialog(alias: target.alias),
    );
    if (confirmed != true || !mounted) return;
    if (service == null) return;
    try {
      await service.remove(target.id);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('${target.alias} eliminado.')),
      );
      _closeWithResult(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(_friendlyFormError(e.code))),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final editing = widget.isEditing;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        title: Text(editing ? 'Editar Beneficiario' : 'Nuevo Beneficiario'),
      ),
      body: _initialFuture != null && !_initialized
          ? FutureBuilder<Beneficiary?>(
              future: _initialFuture,
              builder: (context, snapshot) {
                if (snapshot.connectionState == ConnectionState.waiting) {
                  return const LoadingView(
                    message: 'Cargando beneficiario...',
                  );
                }
                if (snapshot.hasError) {
                  final error = snapshot.error;
                  return ErrorView(
                    message: error is ApiException
                        ? error.message
                        : 'No encontramos ese beneficiario.',
                    retryLabel: 'Volver',
                    onRetry: () => _closeWithResult(false),
                  );
                }
                return _formBody(editing);
              },
            )
          : _formBody(editing),
    );
  }

  Widget _formBody(bool editing) {
    final initial = widget.initial;
    return Column(
      children: [
        Expanded(
          child: SingleChildScrollView(
            key: const Key('beneficiary-form-scroll'),
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (_showDuplicate) ...[
                  _DuplicateBanner(
                    onViewExisting: () => _closeWithResult(false),
                    onDismiss: () =>
                        setState(() => _showDuplicate = false),
                  ),
                  const SizedBox(height: 20),
                ],
                _SectionLabel(
                  title: 'Alias o apodo *',
                  hint: 'Ej: Familiar, Proveedor',
                ),
                const SizedBox(height: 8),
                AppTextField(
                  key: const Key('beneficiary-alias'),
                  label: '',
                  controller: _aliasController,
                  hintText: 'Mamá Lima',
                  errorText: _aliasError,
                  textInputAction: TextInputAction.next,
                ),
                const SizedBox(height: 20),
                const _SectionLabel(title: 'Documento de identidad *'),
                const SizedBox(height: 8),
                _DocTypeTabs(
                  type: _docType,
                  onChanged: (type) => setState(() {
                    _docType = type;
                    _docVerified = false;
                    _docError = null;
                  }),
                ),
                const SizedBox(height: 8),
                Row(
                  children: [
                    Expanded(
                      child: AppTextField(
                        key: const Key('beneficiary-doc-number'),
                        label: '',
                        controller: _docController,
                        hintText: _docType == BeneficiaryDocType.dni
                            ? '42891044'
                            : '20601234567',
                        errorText: _docError,
                        keyboardType: TextInputType.number,
                        textInputAction: TextInputAction.next,
                        onChanged: (_) {
                          if (_docVerified) {
                            setState(() => _docVerified = false);
                          }
                        },
                      ),
                    ),
                    const SizedBox(width: 8),
                    SizedBox(
                      width: 100,
                      height: 56,
                      child: FilledButton.tonal(
                        key: const Key('beneficiary-doc-validate'),
                        onPressed: _validateDoc,
                        child: const Text('Validar'),
                      ),
                    ),
                  ],
                ),
                if (_docVerified) ...[
                  const SizedBox(height: 8),
                  Container(
                    key: const Key('doc-verified-banner'),
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: AppColors.successContainer,
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Row(
                      children: [
                        const Icon(
                          Icons.check_circle_outline,
                          size: 20,
                          color: AppColors.primary,
                        ),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            _docType == BeneficiaryDocType.dni
                                ? 'Formato DNI válido (8 dígitos). El titular se confirmará al guardar.'
                                : 'Formato RUC válido (11 dígitos). La razón social se verificará en SUNAT al guardar.',
                            style: AppTypography.labelMd.copyWith(
                              fontSize: 12,
                              fontWeight: FontWeight.w600,
                              color: AppColors.onSuccessContainer,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
                const SizedBox(height: 20),
                const _SectionLabel(title: 'Entidad financiera *'),
                const SizedBox(height: 8),
                DropdownButtonFormField<String>(
                  key: const Key('beneficiary-bank'),
                  initialValue: _bankCode,
                  decoration: InputDecoration(
                    filled: true,
                    fillColor: AppColors.surfaceContainerLowest,
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                  ),
                  items: [
                    for (final bank in _banks)
                      DropdownMenuItem(
                        value: bank.$1,
                        child: Text(bank.$2),
                      ),
                  ],
                  onChanged: (value) {
                    if (value != null) setState(() => _bankCode = value);
                  },
                ),
                const SizedBox(height: 16),
                const _SectionLabel(title: 'Moneda de la cuenta *'),
                const SizedBox(height: 8),
                Row(
                  children: [
                    _CurrencyOption(
                      key: const Key('beneficiary-currency-pen'),
                      label: 'Soles (S/)',
                      selected: _currency == 'PEN',
                      onTap: () => setState(() => _currency = 'PEN'),
                    ),
                    const SizedBox(width: 8),
                    _CurrencyOption(
                      key: const Key('beneficiary-currency-usd'),
                      label: 'Dólares (\$)',
                      selected: _currency == 'USD',
                      onTap: () => setState(() => _currency = 'USD'),
                    ),
                  ],
                ),
                const SizedBox(height: 16),
                _SectionLabel(
                  title: 'Número de Cuenta o CCI *',
                  hint: '14 dígitos (cuenta) o 20 (CCI)',
                ),
                const SizedBox(height: 8),
                AppTextField(
                  key: const Key('beneficiary-account'),
                  label: '',
                  controller: _accountController,
                  hintText: '00194827104018',
                  errorText: _accountError,
                  // Texto (no numerico restrictivo): el formato —14 con DV
                  // o 20 CCI— se valida en el `validator` (E2-T29); el
                  // teclado no garantiza el formato.
                  keyboardType: TextInputType.text,
                  textInputAction: TextInputAction.done,
                ),
                const SizedBox(height: 20),
                // D1: sin tarjeta de tope. Nota informativa de la unica
                // regla vigente (umbral biometrico general S/ 1,000).
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: const Color(0xFFE4EFE6),
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: const Text(
                    'Las transferencias mayores a S/ 1,000 requieren '
                    'validación biométrica.',
                    style: TextStyle(
                      fontSize: 12,
                      color: AppColors.onSurfaceVariant,
                    ),
                  ),
                ),
                if (editing && initial != null) ...[
                  const SizedBox(height: 20),
                  Center(
                    child: TextButton.icon(
                      key: const Key('beneficiary-delete'),
                      onPressed: () => _delete(initial),
                      icon: const Icon(
                        Icons.delete_outline,
                        size: 18,
                        color: AppColors.error,
                      ),
                      label: const Text(
                        'Eliminar beneficiario de mis contactos',
                        style: TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                          color: AppColors.error,
                        ),
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
        SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
            child: SizedBox(
              width: double.infinity,
              height: 52,
              child: FilledButton.icon(
                key: const Key('beneficiary-save'),
                onPressed: _saving ? null : _save,
                icon: _saving
                    ? const SizedBox.square(
                        dimension: 20,
                        child: CircularProgressIndicator(strokeWidth: 2.5),
                      )
                    : const Icon(Icons.check, size: 20),
                label: Text(
                  editing ? 'Guardar cambios' : 'Guardar beneficiario',
                  style: const TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel({required this.title, this.hint});

  final String title;
  final String? hint;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.spaceBetween,
      children: [
        Text(
          title,
          style: AppTypography.labelMd.copyWith(
            fontSize: 12,
            fontWeight: FontWeight.w600,
            color: AppColors.primaryContainer,
          ),
        ),
        if (hint != null)
          Text(
            hint!,
            style: AppTypography.labelMd.copyWith(
              fontSize: 11,
              color: AppColors.outline,
            ),
          ),
      ],
    );
  }
}

/// Selector DNI/RUC (mockup `Field-Doc` + `Alta 4 - RUC SUNAT`).
class _DocTypeTabs extends StatelessWidget {
  const _DocTypeTabs({required this.type, required this.onChanged});

  final BeneficiaryDocType type;
  final ValueChanged<BeneficiaryDocType> onChanged;

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
          _DocTab(
            key: const Key('doc-type-dni'),
            label: 'DNI (8 dígitos)',
            selected: type == BeneficiaryDocType.dni,
            onTap: () => onChanged(BeneficiaryDocType.dni),
          ),
          _DocTab(
            key: const Key('doc-type-ruc'),
            label: 'RUC (11 dígitos)',
            selected: type == BeneficiaryDocType.ruc,
            onTap: () => onChanged(BeneficiaryDocType.ruc),
          ),
        ],
      ),
    );
  }
}

class _DocTab extends StatelessWidget {
  const _DocTab({
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
              fontWeight:
                  selected ? FontWeight.w600 : FontWeight.w500,
              color: selected
                  ? AppColors.primary
                  : AppColors.onSurfaceVariant,
            ),
          ),
        ),
      ),
    );
  }
}

class _CurrencyOption extends StatelessWidget {
  const _CurrencyOption({
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
          height: 44,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: selected
                ? AppColors.primary
                : AppColors.surfaceContainerLowest,
            borderRadius: BorderRadius.circular(12),
          ),
          child: Text(
            label,
            style: AppTypography.labelMd.copyWith(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: selected
                  ? AppColors.onPrimary
                  : AppColors.onSurfaceVariant,
            ),
          ),
        ),
      ),
    );
  }
}

/// Banner de duplicado (mockup `Alta 2 - Duplicado`).
class _DuplicateBanner extends StatelessWidget {
  const _DuplicateBanner({
    required this.onViewExisting,
    required this.onDismiss,
  });

  final VoidCallback onViewExisting;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context) {
    return Container(
      key: const Key('duplicate-banner'),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: AppColors.warningContainer,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        children: [
          Row(
            children: [
              Container(
                width: 32,
                height: 32,
                decoration: const BoxDecoration(
                  color: AppColors.secondaryContainer,
                  shape: BoxShape.circle,
                ),
                child: const Icon(
                  Icons.warning_amber_rounded,
                  size: 18,
                  color: Colors.white,
                ),
              ),
              const SizedBox(width: 10),
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      mainAxisAlignment: MainAxisAlignment.spaceBetween,
                      children: [
                        Text(
                          'REGISTRO DETECTADO',
                          style: TextStyle(
                            fontSize: 11,
                            fontWeight: FontWeight.bold,
                            color: AppColors.secondary,
                          ),
                        ),
                        Text(
                          'DUPLICATE_BENEFICIARY',
                          style: TextStyle(
                            fontSize: 10,
                            color: AppColors.onWarningContainer,
                          ),
                        ),
                      ],
                    ),
                    SizedBox(height: 2),
                    Text(
                      'Esta cuenta o CCI ya está registrada. ¿Deseas redirigirte a editarla?',
                      style: TextStyle(
                        fontSize: 12,
                        color: AppColors.onSecondaryFixed,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: FilledButton(
                  key: const Key('duplicate-view-existing'),
                  style: FilledButton.styleFrom(
                    backgroundColor: AppColors.primaryContainer,
                  ),
                  onPressed: onViewExisting,
                  child: const Text(
                    'Ver existente',
                    style: TextStyle(fontSize: 12),
                  ),
                ),
              ),
              TextButton(
                key: const Key('duplicate-dismiss'),
                onPressed: onDismiss,
                child: const Text(
                  'Ignorar',
                  style: TextStyle(
                    fontSize: 12,
                    color: AppColors.onWarningContainer,
                  ),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

String _friendlyFormError(String code) {
  switch (code) {
    case 'VALIDATION_ERROR':
      return 'Revisa los datos ingresados e intentalo de nuevo.';
    case 'NOT_AUTHORIZED':
      return 'No tienes permiso para esta operacion.';
    case 'NOT_FOUND':
      return 'No encontramos ese beneficiario.';
    case 'IDEMPOTENCY_CONFLICT':
      return 'Esta operacion ya fue registrada. Revisa tu agenda.';
    default:
      return 'Ocurrio un error inesperado. Intentalo mas tarde.';
  }
}
