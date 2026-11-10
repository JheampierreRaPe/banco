import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../accounts/data/accounts_service.dart';
import '../../accounts/models/account.dart';
import '../models/loan_schedule.dart';
import '../utils/format.dart';
import '../data/credit_disbursement_service.dart';
import '../data/loan_schedule_service.dart';

/// Sección de desembolso del crédito (M-1, HU12; spec
/// `docs/design/creditos-pantallas.md` §8c).
///
/// Punto de entrada ÚNICO del disparador de desembolso: vive en el bloque
/// de éxito de la firma (`credit_sign_page.dart`, expediente `APPROVED` +
/// contrato `SIGNED`) y NO se duplica en el listado (el listado solo muestra
/// préstamos ya desembolsados).
///
/// - Al montar verifica si el expediente ya tiene préstamo desembolsado
///   (`loansService.listLoans()` buscando `application_id`): si lo hay, NO
///   ofrece la CTA y muestra la card `disburse-existing` con `Ver
///   cronograma`. Sin `loansService` degrada a CTA optimista (el `409` del
///   servidor cubre la carrera).
/// - Selector de cuentas del titular (enmascaradas, del servidor): si el
///   usuario no elige, se omite `account_id` y el backend usa la cuenta
///   principal. Sin `accountsService` (o vacío/error) se informa el default
///   sin bloquear la CTA.
/// - CTA `Desembolsar a mi cuenta` (`POST .../disburse` con
///   `ApiClient.postWithIdempotency`: clave nueva por intento). Refleja
///   `loading/success/error` en español accionable; tras el éxito ofrece
///   `Ver cronograma` (→ `/credits/loans/{loanId}`, préstamo recién creado).
///
/// Cliente delgado (`docs/19` §4): muestra montos y cuenta TAL CUAL los
/// devuelve el servidor (sin calcular). Sin PII en logs: la cuenta solo se
/// muestra enmascarada (`account_masked`).
///
/// Los tres servicios son inyectables para tests; `null` degrada a aviso
/// honesto en vez de romper.
///
/// E3-T24: la sección es SOLO reintento (el desembolso automático ya
/// ocurrió al firmar; la página la monta únicamente cuando el automático
/// no liquidó). Con [isRetry] la copia lo dice explícito.
class CreditDisburseSection extends StatefulWidget {
  const CreditDisburseSection({
    super.key,
    required this.applicationId,
    this.disburseService,
    this.loansService,
    this.accountsService,
    this.isRetry = false,
  });

  final String applicationId;
  final CreditDisbursementServiceBase? disburseService;
  final LoanScheduleServiceBase? loansService;
  final AccountsServiceBase? accountsService;
  final bool isRetry;

  @override
  State<CreditDisburseSection> createState() => _CreditDisburseSectionState();
}

class _CreditDisburseSectionState extends State<CreditDisburseSection> {
  bool _checkingLoan = false;
  String? _existingLoanId;

  List<Account>? _accounts;
  String? _selectedAccountId;

  bool _disbursing = false;
  DisbursementResult? _result;
  ApiException? _error;

  String get _applicationId => widget.applicationId.trim();

  @override
  void initState() {
    super.initState();
    _checkExistingLoan();
    _loadAccounts();
  }

  Future<void> _checkExistingLoan() async {
    final loans = widget.loansService;
    if (loans == null || _applicationId.isEmpty) return;
    setState(() => _checkingLoan = true);
    try {
      final items = await loans.listLoans();
      if (!mounted) return;
      Loan? match;
      for (final loan in items) {
        if (loan.applicationId == _applicationId) {
          match = loan;
          break;
        }
      }
      setState(() {
        _checkingLoan = false;
        _existingLoanId = match?.id;
      });
    } catch (_) {
      // Sin lectura no se bloquea el desembolso: el `409`/`200`-replay del
      // servidor cubre el caso ya-desembolsado (M-1).
      if (!mounted) return;
      setState(() => _checkingLoan = false);
    }
  }

  Future<void> _loadAccounts() async {
    final accounts = widget.accountsService;
    if (accounts == null) return;
    try {
      final items = await accounts.getAccounts();
      if (!mounted) return;
      setState(() => _accounts = items);
    } catch (_) {
      // Sin cuentas listadas se usa el default (principal): no se bloquea.
      if (!mounted) return;
      setState(() => _accounts = const []);
    }
  }

  Future<void> _disburse() async {
    final service = widget.disburseService;
    if (service == null || _disbursing || _result != null) return;
    setState(() {
      _disbursing = true;
      _error = null;
    });
    // Clave nueva por intento: si el usuario cambia de cuenta entre
    // reintentos, misma clave + cuerpo distinto devolvería `409`
    // (`IDEMPOTENCY_CONFLICT`; patrón E2-T21/G39).
    final attemptKey = const Uuid().v4();
    try {
      final result = await service.disburseApplication(
        applicationId: _applicationId,
        accountId: _selectedAccountId,
        idempotencyKey: attemptKey,
      );
      if (!mounted) return;
      setState(() {
        _disbursing = false;
        _result = result;
        _error = null;
      });
    } on ApiException catch (error) {
      if (!mounted) return;
      setState(() {
        _disbursing = false;
        _error = error;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _disbursing = false;
        _error = ApiException(
          code: 'UNKNOWN',
          message: 'Ocurrió un error inesperado. Inténtalo más tarde.',
        );
      });
    }
  }

  bool get _isConflict {
    final code = (_error?.code ?? '').toUpperCase();
    return code == 'CONFLICT' ||
        code == 'IDEMPOTENCY_CONFLICT' ||
        code == 'DUPLICATE_REQUEST';
  }

  @override
  Widget build(BuildContext context) {
    if (_applicationId.isEmpty) return const SizedBox.shrink();
    final service = widget.disburseService;
    if (service == null) {
      return Container(
        key: const Key('disburse-unavailable'),
        width: double.infinity,
        padding: const EdgeInsets.all(AppSpacing.containerPadding),
        decoration: BoxDecoration(
          color: AppColors.surfaceContainerLow,
          borderRadius: BorderRadius.circular(AppRadii.lg),
        ),
        child: Text(
          'El desembolso no está disponible por ahora.',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
      );
    }
    if (_checkingLoan) {
      return Container(
        key: const Key('disburse-checking'),
        width: double.infinity,
        padding: const EdgeInsets.all(AppSpacing.containerPadding),
        decoration: BoxDecoration(
          color: AppColors.surfaceContainerLow,
          borderRadius: BorderRadius.circular(AppRadii.lg),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            const LinearProgressIndicator(),
            const SizedBox(height: AppSpacing.stackSm),
            Text(
              'Verificando el estado del desembolso…',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
            ),
          ],
        ),
      );
    }
    final existing = _existingLoanId;
    if (existing != null && existing.isNotEmpty) {
      return _alreadyDisbursedCard(existing);
    }
    final result = _result;
    if (result != null) return _successCard(result);
    return _disburseCard();
  }

  Widget _alreadyDisbursedCard(String loanId) {
    return Container(
      key: const Key('disburse-existing'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.successContainer,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Desembolso completado',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.onSuccessContainer,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Este crédito ya fue desembolsado. Revisa tu cronograma.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSuccessContainer,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('disburse-schedule-cta'),
              onPressed: () => context.push('/credits/loans/$loanId'),
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
              ),
              child: const Text('Ver cronograma'),
            ),
          ),
        ],
      ),
    );
  }

  Widget _disburseCard() {
    return Container(
      key: const Key('disburse-section'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLow,
        borderRadius: BorderRadius.circular(AppRadii.lg),
        border: Border.all(color: AppColors.primary, width: 1.5),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            widget.isRetry
                ? 'Reintentar desembolso'
                : 'Desembolso a tu cuenta',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            widget.isRetry
                ? 'El desembolso automático no se completó. '
                    'Reinténtalo aquí a tu cuenta principal.'
                : 'El dinero se acredita en la cuenta que elijas.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          _accountSelector(),
          const SizedBox(height: AppSpacing.stackSm),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              key: const Key('disburse-cta'),
              onPressed: _disbursing ? null : _disburse,
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: Text(
                _disbursing
                    ? 'Desembolsando…'
                    : (widget.isRetry
                        ? 'Reintentar desembolso'
                        : 'Desembolsar a mi cuenta'),
              ),
            ),
          ),
          if (_error != null) ...[
            const SizedBox(height: AppSpacing.stackSm),
            Container(
              key: const Key('disburse-error'),
              width: double.infinity,
              padding: const EdgeInsets.all(AppSpacing.containerPadding),
              decoration: BoxDecoration(
                color: AppColors.errorContainer,
                borderRadius: BorderRadius.circular(AppRadii.md),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    creditDisbursementUserMessage(_error!),
                    style: AppTypography.bodyMd.copyWith(
                      color: AppColors.onErrorContainer,
                    ),
                  ),
                  if (_isConflict) ...[
                    const SizedBox(height: AppSpacing.stackSm),
                    SizedBox(
                      width: double.infinity,
                      child: OutlinedButton(
                        key: const Key('disburse-loans-cta'),
                        onPressed: () => context.push('/credits/loans'),
                        style: OutlinedButton.styleFrom(
                          minimumSize: const Size(88, 48),
                        ),
                        child: const Text('Ver mis préstamos'),
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }

  Widget _accountSelector() {
    final service = widget.accountsService;
    final accounts = _accounts;
    if (service == null || (accounts != null && accounts.isEmpty)) {
      return Text(
        key: const Key('disburse-note'),
        'Se desembolsará a tu cuenta principal.',
        style: AppTypography.bodyMd.copyWith(
          color: AppColors.secondaryText,
        ),
      );
    }
    if (accounts == null) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          const LinearProgressIndicator(),
          const SizedBox(height: 4),
          Text(
            'Cargando tus cuentas…',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ],
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          'Cuenta de desembolso',
          style: AppTypography.labelMd.copyWith(
            fontWeight: FontWeight.w600,
            color: AppColors.primary,
          ),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        DropdownButtonFormField<String>(
          key: const Key('disburse-account-selector'),
          initialValue: _selectedAccountId,
          hint: const Text('Cuenta principal (por defecto)'),
          items: [
            for (final account in accounts)
              DropdownMenuItem(
                value: account.id,
                child: Text(
                  '${account.type} ${account.accountNumberMasked}',
                  overflow: TextOverflow.ellipsis,
                ),
              ),
          ],
          onChanged: _disbursing
              ? null
              : (value) => setState(() => _selectedAccountId = value),
          decoration: InputDecoration(
            filled: true,
            fillColor: AppColors.surfaceContainerLow,
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(AppRadii.md),
              borderSide: BorderSide.none,
            ),
          ),
        ),
        const SizedBox(height: 4),
        Text(
          'Si no eliges, usamos tu cuenta principal.',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
      ],
    );
  }

  Widget _successCard(DisbursementResult result) {
    final masked = (result.accountMasked ?? '').trim();
    final amount = result.amountMinor;
    final currency = (result.currency ?? 'PEN').trim();
    return Container(
      key: const Key('disburse-success'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.successContainer,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '¡Desembolso acreditado!',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.onSuccessContainer,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            amount != null
                ? 'Se acreditó ${formatMinor(amount, currency)}'
                    '${masked.isNotEmpty ? ' en tu cuenta $masked.' : '.'}'
                : 'El dinero ya está en tu cuenta'
                    '${masked.isNotEmpty ? ' $masked.' : '.'}',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSuccessContainer,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          SizedBox(
            width: double.infinity,
            child: FilledButton(
              key: const Key('disburse-schedule-cta'),
              onPressed: () =>
                  context.push('/credits/loans/${result.loanId}'),
              style: FilledButton.styleFrom(
                minimumSize: const Size(88, 52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(AppRadii.md),
                ),
              ),
              child: const Text('Ver cronograma'),
            ),
          ),
        ],
      ),
    );
  }
}
