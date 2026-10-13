import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../data/credit_applications_service.dart';
import '../models/credit.dart';
import '../models/loan_application.dart';
import '../utils/format.dart';

/// Pantalla de solicitud de credito + consentimiento (E3-T09, HU10; spec
/// `docs/design/creditos-pantallas.md` §2, mockup `creditos-solicitud.png`).
///
/// - Header `Tu solicitud` + `Revisamos tus datos y te damos un dictamen en
///   minutos.`
/// - Card resumen (`VIENES DE SIMULAR` con producto/monto/plazo/cuota del
///   [SimulationDraft], sin recalcular) + link `Ajustar simulación`.
/// - Card Datos: documento solo lectura enmascarado, ingreso mensual neto
///   (obligatorio, > 0), deudas totales (opcional, default 0: el contrato
///   `POST /credits/applications` exige `total_debt_minor >= 0`), cuenta de
///   desembolso solo lectura enmascarada, monto y plazo (pre-cargados del
///   simulador). El documento y la cuenta NO viajan al backend (el servidor
///   usa `extra="forbid"` y los rechaza con `422`).
/// - Card Consentimiento (borde `primary`, checkbox obligatorio): sin marcar,
///   el CTA queda bloqueado; la validacion autoritativa es del backend.
/// - Envio con `ApiClient.postWithIdempotency`: la misma `Idempotency-Key`
///   ante reintentos del mismo intento (cambia solo si cambia el cuerpo, para
///   no chocar con `409 IDEMPOTENCY_CONFLICT`).
/// - `422`/`404`/`409` -> mensajes accionables en espanol;
///   `BUREAU_UNAVAILABLE` -> navega a `En revision` (no es error).
///
/// Cliente delgado (`docs/19` §4): envia datos y muestra el dictamen del
/// servidor; no decide elegibilidad ni calcula DTI/score.
///
/// [service] es inyectable para tests; la ruta lo resuelve desde
/// `creditApplicationsServiceFactory`. `null` no rompe la pantalla: el
/// formulario se muestra igual y el error aparece al enviar.
///
/// M10: [documentMasked]/[disburseAccountMasked] son SOLO lectura y la
/// ruta NO los inyecta (no hay fuente real: `GET /me` solo trae
/// nombre/apellido, sin documento ni cuentas; ver `profile_service.dart`).
/// Por eso el constructor trae valores de EJEMPLO y [isExampleData] (default
/// `true`) los etiqueta en la UI como "Dato de ejemplo". Cuando exista una
/// fuente real, la ruta la inyectara con `isExampleData: false` y el aviso
/// desaparece. Jamas se presentan como verificados ni viajan al backend.
class CreditApplicationPage extends StatefulWidget {
  const CreditApplicationPage({
    super.key,
    this.draft,
    this.service,
    this.documentMasked = 'DNI •••• 4521',
    this.disburseAccountMasked = 'Ahorros •••• 4521',
    this.isExampleData = true,
  });

  final SimulationDraft? draft;
  final CreditApplicationsServiceBase? service;

  /// Documento YA enmascarado (solo lectura; nunca en claro ni en logs).
  final String documentMasked;

  /// Cuenta de desembolso YA enmascarada (solo lectura; informativa).
  final String disburseAccountMasked;

  /// `true` = los valores de arriba son de ejemplo (sin fuente real) y la
  /// UI lo declara. `false` = vienen de una fuente real del titular.
  final bool isExampleData;

  @override
  State<CreditApplicationPage> createState() => _CreditApplicationPageState();
}

class _CreditApplicationPageState extends State<CreditApplicationPage> {
  late final TextEditingController _incomeController;
  late final TextEditingController _debtController;
  late final TextEditingController _amountController;
  late final TextEditingController _termController;

  bool _consent = false;
  bool _attempted = false;
  bool _submitting = false;
  ApiException? _serverError;

  /// Clave del intento en curso (misma ante reintentos; nueva si el cuerpo
  /// cambia, para no provocar `409 IDEMPOTENCY_CONFLICT`).
  String? _idempotencyKey;
  Map<String, dynamic>? _lastBody;

  @override
  void initState() {
    super.initState();
    final draft = widget.draft;
    _incomeController = TextEditingController();
    _debtController = TextEditingController();
    _amountController = TextEditingController(
      text: draft == null ? '' : formatMinor(draft.amountMinor, 'PEN'),
    );
    _termController = TextEditingController(
      text: draft == null ? '' : '${draft.termMonths}',
    );
  }

  @override
  void dispose() {
    _incomeController.dispose();
    _debtController.dispose();
    _amountController.dispose();
    _termController.dispose();
    super.dispose();
  }

  // --validacion_UX_(el_backend_manda)----------------------------------------

  /// Primer campo faltante/invalido, o `null` si el formulario esta listo.
  ///
  /// Orden: producto (sin borrador no hay `product_id` que enviar),
  /// ingreso, deudas, monto, plazo.
  String? _firstProblem() {
    if (widget.draft == null) {
      return 'Vuelve al simulador para elegir un producto antes de enviar.';
    }
    final income = parseAmountMinor(_incomeController.text);
    if (income == null || income <= 0) {
      return 'Ingresa tu ingreso mensual neto (mayor a S/ 0.00).';
    }
    final debtText = _debtController.text.trim();
    if (debtText.isNotEmpty) {
      final debt = parseAmountMinor(debtText);
      if (debt == null) {
        return 'Revisa las deudas totales (0 o un monto válido).';
      }
    }
    final amount = parseAmountMinor(_amountController.text);
    if (amount == null || amount <= 0) {
      return 'Ingresa el monto solicitado (mayor a S/ 0.00).';
    }
    final term = int.tryParse(_termController.text.trim());
    if (term == null || term <= 0) {
      return 'Ingresa el plazo en meses (mayor a 0).';
    }
    return null;
  }

  String? _incomeError() {
    if (!_attempted) return null;
    final income = parseAmountMinor(_incomeController.text);
    if (income == null || income <= 0) {
      return 'Obligatorio, mayor a S/ 0.00.';
    }
    return null;
  }

  String? _debtError() {
    if (!_attempted) return null;
    final debtText = _debtController.text.trim();
    if (debtText.isEmpty) return null;
    if (parseAmountMinor(debtText) == null) {
      return 'Monto inválido (usa 0 si no tienes deudas).';
    }
    return null;
  }

  String? _amountError() {
    if (!_attempted) return null;
    final amount = parseAmountMinor(_amountController.text);
    if (amount == null || amount <= 0) {
      return 'Obligatorio, mayor a S/ 0.00.';
    }
    return null;
  }

  String? _termError() {
    if (!_attempted) return null;
    final term = int.tryParse(_termController.text.trim());
    if (term == null || term <= 0) {
      return 'Obligatorio, mayor a 0.';
    }
    return null;
  }

  LoanApplicationRequest? _buildRequest() {
    final draft = widget.draft;
    if (draft == null) return null;
    final income = parseAmountMinor(_incomeController.text);
    final debtText = _debtController.text.trim();
    final debt =
        debtText.isEmpty ? 0 : parseAmountMinor(debtText);
    final amount = parseAmountMinor(_amountController.text);
    final term = int.tryParse(_termController.text.trim());
    if (income == null ||
        income <= 0 ||
        debt == null ||
        amount == null ||
        amount <= 0 ||
        term == null ||
        term <= 0) {
      return null;
    }
    return LoanApplicationRequest(
      productId: draft.productId,
      requestedAmountMinor: amount,
      termMonths: term,
      totalIncomeMinor: income,
      totalDebtMinor: debt,
      bureauAuthorized: true,
    );
  }

  // --envio_idempotente---------------------------------------------------------

  Future<void> _submit() async {
    if (_submitting) return;
    setState(() {
      _attempted = true;
      _serverError = null;
    });
    if (!_consent) return;
    final request = _buildRequest();
    if (request == null) return;
    final service = widget.service;
    if (service == null) {
      setState(() {
        _serverError = ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de créditos no configurado.',
        );
      });
      return;
    }
    final body = request.toJson();
    if (_idempotencyKey == null || !mapEquals(_lastBody, body)) {
      _idempotencyKey = const Uuid().v4();
      _lastBody = Map<String, dynamic>.from(body);
    }
    final key = _idempotencyKey!;
    setState(() => _submitting = true);
    try {
      final result = await service.submitApplication(
        request: request,
        idempotencyKey: key,
      );
      if (!mounted) return;
      context.push('/credits/status', extra: result);
    } on ApiException catch (error) {
      if (!mounted) return;
      if (error.code == 'BUREAU_UNAVAILABLE') {
        // El bureau caido NO es error visible: el caso pasa a revision
        // manual (`docs/05` §6.4).
        context.push(
          '/credits/status',
          extra: LoanApplicationResult.inReviewNotice(
            reason: creditApplicationUserMessage(error),
          ),
        );
        return;
      }
      setState(() => _serverError = error);
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _serverError = ApiException(
          code: 'UNKNOWN',
          message: 'Ocurrió un error inesperado. Inténtalo más tarde.',
        );
      });
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  void _onFieldChanged(String _) {
    if (_attempted || _serverError != null) {
      setState(() => _serverError = null);
    }
  }

  // --build--------------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    final draft = widget.draft;
    final problem = _firstProblem();
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Tu solicitud')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        children: [
          Text(
            'Tu solicitud',
            style: AppTypography.headlineSm.copyWith(
              color: AppColors.primary,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Revisamos tus datos y te damos un dictamen en minutos.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _summaryCard(draft),
          const SizedBox(height: AppSpacing.stackMd),
          _dataCard(problem),
          const SizedBox(height: AppSpacing.stackMd),
          _consentCard(),
          const SizedBox(height: AppSpacing.stackMd),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              key: const Key('credit-apply-submit'),
              onPressed: (!_consent || _submitting) ? null : _submit,
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: _submitting
                  ? const SizedBox(
                      key: Key('credit-apply-submitting'),
                      width: 22,
                      height: 22,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Text('Enviar solicitud'),
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Sin marcar el consentimiento el botón se muestra bloqueado. '
            'El envío usa clave de idempotencia (reintentos seguros).',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
          if (_serverError != null) ...[
            const SizedBox(height: AppSpacing.stackMd),
            _serverErrorCard(_serverError!),
          ],
        ],
      ),
    );
  }

  Widget _summaryCard(SimulationDraft? draft) {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'VIENES DE SIMULAR',
            style: AppTypography.labelSm.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            key: const Key('credit-apply-summary'),
            draft == null
                ? 'Sin simulación previa.'
                : '${draft.productName} • '
                    '${formatMinor(draft.amountMinor, 'PEN')} • '
                    '${draft.termMonths} m • '
                    '${formatMinor(draft.paymentMinor, 'PEN')}/mes',
            style: AppTypography.bodyLg.copyWith(
              color: AppColors.onSurface,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              onPressed: () => context.pop(),
              child: Text(
                'Ajustar simulación',
                style: AppTypography.labelMd.copyWith(
                  color: AppColors.accentText,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _dataCard(String? problem) {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _readonlyField(
            label: 'Documento',
            valueKey: const Key('credit-apply-document'),
            value: widget.documentMasked,
            // M10: sin fuente real, aviso honesto de dato de ejemplo.
            caption: widget.isExampleData
                ? 'Dato de ejemplo: aún no recibimos tu documento.'
                : null,
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _moneyField(
            key: const Key('credit-apply-income'),
            label: 'Ingreso mensual neto (S/)',
            controller: _incomeController,
            hintText: '3,500.00',
            errorText: _incomeError(),
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _moneyField(
            key: const Key('credit-apply-debt'),
            label: 'Deudas mensuales totales (S/, opcional)',
            controller: _debtController,
            hintText: '0.00',
            errorText: _debtError(),
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _readonlyField(
            label: 'Cuenta de desembolso',
            valueKey: const Key('credit-apply-account'),
            value: widget.disburseAccountMasked,
            // M10: sin fuente real, aviso honesto de dato de ejemplo.
            caption: widget.isExampleData
                ? 'Dato de ejemplo: aún no recibimos tu cuenta.'
                : null,
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _moneyField(
            key: const Key('credit-apply-amount'),
            label: 'Monto solicitado (S/)',
            controller: _amountController,
            hintText: '10,000.00',
            errorText: _amountError(),
          ),
          const SizedBox(height: AppSpacing.stackMd),
          _termField(),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            key: const Key('credit-apply-inline-notice'),
            _attempted && problem != null
                ? problem
                : 'Si falta un dato verás aquí el aviso antes de enviar.',
            style: AppTypography.bodyMd.copyWith(
              color: _attempted && problem != null
                  ? AppColors.error
                  : AppColors.secondaryText,
            ),
          ),
        ],
      ),
    );
  }

  /// Fila solo lectura enmascarada (documento / cuenta): se muestra tal cual,
  /// nunca en claro. [caption] (M10) declara el dato de ejemplo cuando no
  /// hay fuente real del titular.
  Widget _readonlyField({
    required String label,
    required Key valueKey,
    required String value,
    String? caption,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          label,
          style: AppTypography.labelMd.copyWith(color: AppColors.primary),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        Container(
          key: valueKey,
          width: double.infinity,
          padding: const EdgeInsets.symmetric(
            horizontal: AppSpacing.stackMd,
            vertical: AppSpacing.stackSm + AppSpacing.unit,
          ),
          decoration: BoxDecoration(
            color: AppColors.surfaceContainerLow,
            borderRadius: BorderRadius.circular(AppRadii.md),
            border: Border.all(color: AppColors.outlineVariant),
          ),
          child: Text(
            value,
            style: AppTypography.bodyLg.copyWith(
              color: AppColors.onSurface,
            ),
          ),
        ),
        if (caption != null) ...[
          const SizedBox(height: 4),
          Text(
            caption,
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              color: AppColors.secondaryText,
              fontStyle: FontStyle.italic,
            ),
          ),
        ],
      ],
    );
  }

  Widget _moneyField({
    required Key key,
    required String label,
    required TextEditingController controller,
    required String hintText,
    required String? errorText,
  }) {
    return Column(
      key: key,
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          label,
          style: AppTypography.labelMd.copyWith(color: AppColors.primary),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        TextField(
          controller: controller,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          textInputAction: TextInputAction.next,
          onChanged: _onFieldChanged,
          style: AppTypography.bodyLg.copyWith(
            color: Theme.of(context).colorScheme.onSurface,
          ),
          decoration: InputDecoration(
            hintText: hintText,
            errorText: errorText,
            filled: true,
            fillColor: AppColors.surfaceContainerLowest,
            hintStyle: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
            errorStyle: AppTypography.labelSm.copyWith(
              color: AppColors.error,
            ),
            contentPadding: const EdgeInsets.symmetric(
              horizontal: AppSpacing.stackMd,
              vertical: AppSpacing.stackSm + AppSpacing.unit,
            ),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: const BorderSide(color: AppColors.outlineVariant),
            ),
            focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: const BorderSide(
                color: AppColors.primary,
                width: 2,
              ),
            ),
            errorBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: const BorderSide(color: AppColors.error),
            ),
            focusedErrorBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: const BorderSide(
                color: AppColors.error,
                width: 2,
              ),
            ),
          ),
        ),
      ],
    );
  }

  Widget _termField() {
    return Column(
      key: const Key('credit-apply-term'),
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          'Plazo (meses)',
          style: AppTypography.labelMd.copyWith(color: AppColors.primary),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        TextField(
          controller: _termController,
          keyboardType: TextInputType.number,
          textInputAction: TextInputAction.done,
          onChanged: _onFieldChanged,
          style: AppTypography.bodyLg.copyWith(
            color: Theme.of(context).colorScheme.onSurface,
          ),
          decoration: InputDecoration(
            hintText: '12',
            errorText: _termError(),
            filled: true,
            fillColor: AppColors.surfaceContainerLowest,
            hintStyle: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
            errorStyle: AppTypography.labelSm.copyWith(
              color: AppColors.error,
            ),
            contentPadding: const EdgeInsets.symmetric(
              horizontal: AppSpacing.stackMd,
              vertical: AppSpacing.stackSm + AppSpacing.unit,
            ),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: const BorderSide(color: AppColors.outlineVariant),
            ),
            focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: const BorderSide(
                color: AppColors.primary,
                width: 2,
              ),
            ),
            errorBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: const BorderSide(color: AppColors.error),
            ),
            focusedErrorBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: const BorderSide(
                color: AppColors.error,
                width: 2,
              ),
            ),
          ),
        ),
      ],
    );
  }

  Widget _consentCard() {
    // `Material` propio (no `Container`+`DecoratedBox`): el `ListTile`
    // pinta su fondo/ink en el `Material` ancestro mas cercano.
    return Material(
      color: AppColors.surfaceContainerLowest,
      elevation: 2,
      shadowColor: const Color(0x0D2B4034),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(AppRadii.lg),
        side: const BorderSide(color: AppColors.primary, width: 1.5),
      ),
      child: CheckboxListTile(
        key: const Key('credit-apply-consent'),
        value: _consent,
        onChanged: _submitting
            ? null
            : (value) => setState(() => _consent = value ?? false),
        controlAffinity: ListTileControlAffinity.leading,
        contentPadding: const EdgeInsets.symmetric(
          horizontal: AppSpacing.stackSm,
          vertical: AppSpacing.stackSm,
        ),
        title: Text(
          'Autorizo la consulta en centrales de riesgo *',
          style: AppTypography.bodyLg.copyWith(
            color: AppColors.onSurface,
            fontWeight: FontWeight.w600,
          ),
        ),
        subtitle: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const SizedBox(height: 4),
            Text(
              'Autorizo expresamente a la entidad a consultar mi historial '
              'en las centrales de riesgo para evaluar esta solicitud. Esta '
              'autorización queda registrada con fecha y hora.',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.onSurface,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              'Obligatorio. Sin esta autorización no se puede enviar.',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _serverErrorCard(ApiException error) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.errorContainer,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            key: const Key('credit-apply-server-error'),
            creditApplicationUserMessage(error),
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onErrorContainer,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          FilledButton(
            key: const Key('credit-apply-retry'),
            onPressed: _submitting ? null : _submit,
            child: const Text('Reintentar'),
          ),
        ],
      ),
    );
  }
}
