import 'dart:async';

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
import '../data/credits_service.dart';
import '../models/credit.dart';
import '../utils/format.dart';

/// Pantalla del simulador de creditos (E3-T04, HU09; spec
/// `docs/design/creditos-pantallas.md` §1, mockup `creditos-simulador.png`).
///
/// - Header `Simula tu crédito` + `El cálculo lo hace el servidor. Sin
///   compromiso.`
/// - Card Producto (chips con nombre + TEA, default: primero).
/// - Card Monto (slider con el rango del producto, default S/ 10,000).
/// - Card Plazo (chips del producto, default 12).
/// - Card Seguro de desgravamen (toggle, default OFF; re-simula con
///   `insurance`).
/// - Card Cuota (fondo ocre: cuota + `TEA • TCEA • Francesa`, todo del
///   servidor) con skeleton durante la peticion.
/// - Card Cronograma resumido (3 filas + `Ver tabla completa`).
/// - Nota + CTA `Solicitar este crédito` (gancho a `/credits/apply`, E3-T09)
///   + CTA `Descargar simulación (PDF)` (501 -> mensaje claro).
///
/// Cliente delgado (`docs/19` §4): cada cambio re-simula contra
/// `POST /credits/simulate` (debounce 400 ms, sin `Idempotency-Key`); la UI
/// muestra la cuota/cronograma del mock del backend sin recalcular nada.
/// 4 estados (`docs/20` §7) con `LoadingView`/`EmptyView`/`ErrorView`.
///
/// [service] es inyectable para tests; la ruta lo resuelve desde
/// `creditsServiceFactory` (cableada por el orquestador). `null` degrada a
/// un error accionable en vez de romper.
///
/// [embedded] (E3-T23): cuando la home `/credits` la monta como pestaña
/// `Simulador`, no crea su propio `Scaffold`/`AppBar` (la home ya aporta el
/// TopBar `Créditos` + el `SegmentedButton`); por defecto es `false` y la
/// página se comporta igual que antes.
///
/// i3 (auditoría): la rama `embedded: false` (standalone con `Scaffold`
/// propio) NO es código muerto: es el modo que ejercitan los tests de
/// widget (`test/credits_simulator_test.dart` la montan sin `embedded`) y
/// queda disponible para una ruta directa del simulador. Se conserva.
class CreditsSimulatorPage extends StatefulWidget {
  const CreditsSimulatorPage({super.key, this.service, this.embedded = false});

  final CreditsServiceBase? service;
  final bool embedded;

  @override
  State<CreditsSimulatorPage> createState() => _CreditsSimulatorPageState();
}

class _CreditsSimulatorPageState extends State<CreditsSimulatorPage> {
  late Future<List<LoanProduct>> _productsFuture;

  @override
  void initState() {
    super.initState();
    _productsFuture = _loadProducts();
    // Sin suscriptor hasta que el FutureBuilder enganche: marcar como
    // manejado evita reportes de zona (mismo patron que `home_page.dart`).
    _productsFuture.ignore();
  }

  Future<List<LoanProduct>> _loadProducts() {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de créditos no configurado.',
        ),
      );
    }
    return service.products();
  }

  void _retry() {
    final future = _loadProducts();
    future.ignore();
    setState(() {
      _productsFuture = future;
    });
  }

  @override
  Widget build(BuildContext context) {
    final content = FutureBuilder<List<LoanProduct>>(
      future: _productsFuture,
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const LoadingView(
            key: Key('credits-loading'),
            message: 'Cargando productos...',
          );
        }
        if (snapshot.hasError) {
          final error = snapshot.error;
          return ErrorView(
            key: const Key('credits-error'),
            message: error is ApiException
                ? creditUserMessage(error)
                : 'Ocurrió un error al consultar el servidor.',
            onRetry: _retry,
          );
        }
        final products = snapshot.data ?? const <LoanProduct>[];
        if (products.isEmpty) {
          return const EmptyView(
            key: Key('credits-empty'),
            message: 'Aún no hay productos de crédito disponibles.',
          );
        }
        return _SimulatorBody(
          key: const Key('credits-simulator'),
          products: products,
          service: widget.service!,
        );
      },
    );
    if (widget.embedded) return content;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Simulador de crédito')),
      body: content,
    );
  }
}

/// Cuerpo del simulador (productos ya cargados): controles + resultado.
class _SimulatorBody extends StatefulWidget {
  const _SimulatorBody({
    super.key,
    required this.products,
    required this.service,
  });

  final List<LoanProduct> products;
  final CreditsServiceBase service;

  @override
  State<_SimulatorBody> createState() => _SimulatorBodyState();
}

class _SimulatorBodyState extends State<_SimulatorBody> {
  /// Debounce de re-simulacion al mover controles (E3-T04).
  static const _debounceDelay = Duration(milliseconds: 400);

  /// Monto por defecto (S/ 10,000 en centimos), ajustado al rango.
  static const _defaultAmountMinor = 1000000;

  /// Plazo por defecto (meses), si el producto lo permite.
  static const _defaultTermMonths = 12;

  /// Plazos de respaldo cuando el producto no trae `allowed_terms`.
  static const _fallbackTerms = [6, 12, 24, 36];

  int _productIndex = 0;
  late int _amountMinor;
  late int _termMonths;
  bool _insurance = false;

  SimulationResult? _result;
  Object? _simError;
  bool _simulating = false;
  bool _exporting = false;
  Timer? _debounce;
  int _requestSeq = 0;

  LoanProduct get _product => widget.products[_productIndex];

  @override
  void initState() {
    super.initState();
    _amountMinor = _clampAmount(_defaultAmountMinor, _product);
    _termMonths = _defaultTerm(_product);
    _simulateNow();
  }

  @override
  void dispose() {
    _debounce?.cancel();
    super.dispose();
  }

  // --_defaults_y_rangos----------------------------------------------------

  static int _clampAmount(int amount, LoanProduct product) {
    return amount.clamp(
      product.minAmountMinor,
      product.maxAmountMinor,
    );
  }

  /// Plazos seleccionables del producto (`allowed_terms` o respaldo en rango).
  static List<int> termsFor(LoanProduct product) {
    final allowed = product.allowedTerms;
    if (allowed != null && allowed.isNotEmpty) return allowed;
    final inRange = _fallbackTerms
        .where((t) =>
            t >= product.minTermMonths && t <= product.maxTermMonths)
        .toList();
    if (inRange.isNotEmpty) return inRange;
    return [product.minTermMonths];
  }

  static int _defaultTerm(LoanProduct product) {
    final terms = termsFor(product);
    if (terms.contains(_defaultTermMonths)) return _defaultTermMonths;
    return terms.first;
  }

  /// Paso del slider (S/ 100; S/ 1 si el rango es pequeno).
  int get _amountStepMinor {
    final range = _product.maxAmountMinor - _product.minAmountMinor;
    return range >= 1000000 ? 10000 : 100;
  }

  int get _amountDivisions {
    final range = _product.maxAmountMinor - _product.minAmountMinor;
    if (range <= 0) return 1;
    return (range / _amountStepMinor).round().clamp(1, 2000);
  }

  // --simulacion_(servidor)---------------------------------------------------

  void _scheduleSimulate() {
    _debounce?.cancel();
    setState(() => _simulating = true);
    _debounce = Timer(_debounceDelay, _simulateNow);
  }

  void _simulateNow() {
    _debounce?.cancel();
    setState(() {
      _simulating = true;
      _simError = null;
    });
    _simulate();
  }

  Future<void> _simulate() async {
    final seq = ++_requestSeq;
    final product = _product;
    final amount = _amountMinor;
    final term = _termMonths;
    final insurance = _insurance;
    try {
      final result = await widget.service.simulate(
        productId: product.id,
        amountMinor: amount,
        termMonths: term,
        insurance: insurance,
      );
      if (!mounted || seq != _requestSeq) return;
      setState(() {
        _result = result;
        _simError = null;
        _simulating = false;
      });
    } catch (error) {
      if (!mounted || seq != _requestSeq) return;
      setState(() {
        _simError = error;
        _simulating = false;
      });
    }
  }

  // --controles---------------------------------------------------------------

  void _onProductSelected(int index) {
    if (index == _productIndex) return;
    setState(() {
      _productIndex = index;
      _amountMinor = _clampAmount(_amountMinor, _product);
      final terms = termsFor(_product);
      if (!terms.contains(_termMonths)) {
        _termMonths = _defaultTerm(_product);
      }
    });
    _scheduleSimulate();
  }

  void _onAmountChanged(double value) {
    final snapped =
        (value / _amountStepMinor).round() * _amountStepMinor;
    final clamped = _clampAmount(snapped, _product);
    if (clamped == _amountMinor) return;
    setState(() => _amountMinor = clamped);
    _scheduleSimulate();
  }

  void _onTermSelected(int term) {
    if (term == _termMonths) return;
    setState(() => _termMonths = term);
    _scheduleSimulate();
  }

  void _onInsuranceChanged(bool value) {
    if (value == _insurance) return;
    setState(() => _insurance = value);
    _scheduleSimulate();
  }

  Future<void> _export() async {
    if (_exporting || _result == null) return;
    setState(() => _exporting = true);
    try {
      final file = await widget.service.exportSimulation(
        productId: _product.id,
        amountMinor: _amountMinor,
        termMonths: _termMonths,
        insurance: _insurance,
      );
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            'Simulación ${file.suggestedFilename} '
            '(${file.bytes.length} bytes). '
            'El guardado nativo está pendiente.',
          ),
        ),
      );
    } on ApiException catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(creditUserMessage(error))),
      );
    } catch (_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('No se pudo descargar la simulación.'),
        ),
      );
    } finally {
      if (mounted) setState(() => _exporting = false);
    }
  }

  void _goToApply() {
    final result = _result;
    if (result == null) return;
    context.push(
      '/credits/apply',
      extra: SimulationDraft(
        productId: _product.id,
        productName: _product.name,
        amountMinor: _amountMinor,
        termMonths: _termMonths,
        insurance: _insurance,
        paymentMinor: result.paymentMinor,
      ),
    );
  }

  void _showFullSchedule(SimulationResult result) {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (context) => _FullScheduleSheet(result: result),
    );
  }

  // --build--------------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
      children: [
        Text(
          'Simula tu crédito',
          style: AppTypography.headlineSm.copyWith(
            color: AppColors.primary,
            fontWeight: FontWeight.w600,
          ),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        Text(
          'El cálculo lo hace el servidor. Sin compromiso.',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
        const SizedBox(height: AppSpacing.stackMd),
        _productCard(),
        const SizedBox(height: AppSpacing.stackMd),
        _amountCard(),
        const SizedBox(height: AppSpacing.stackMd),
        _termCard(),
        const SizedBox(height: AppSpacing.stackMd),
        _insuranceCard(),
        const SizedBox(height: AppSpacing.stackMd),
        _quotaCard(),
        const SizedBox(height: AppSpacing.stackMd),
        if (_result != null && _simError == null) _scheduleCard(_result!),
        if (_result != null && _simError == null)
          const SizedBox(height: AppSpacing.stackMd),
        Text(
          'Simulación sin registro crediticio. Sin compromiso.',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
        const SizedBox(height: AppSpacing.stackMd),
        SizedBox(
          width: double.infinity,
          child: FilledButton(
            key: const Key('credit-apply-cta'),
            onPressed: _result == null ? null : _goToApply,
            style: FilledButton.styleFrom(
              minimumSize: const Size(88, 52),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
              ),
            ),
            child: const Text('Solicitar este crédito'),
          ),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        SizedBox(
          width: double.infinity,
          child: OutlinedButton.icon(
            key: const Key('credit-export-cta'),
            onPressed:
                (_result == null || _exporting) ? null : _export,
            icon: _exporting
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.download_outlined, size: 18),
            label: const Text('Descargar simulación (PDF)'),
            style: OutlinedButton.styleFrom(
              minimumSize: const Size(88, 52),
            ),
          ),
        ),
      ],
    );
  }

  Widget _productCard() {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Producto',
            style: AppTypography.labelSm.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (var i = 0; i < widget.products.length; i++)
                ChoiceChip(
                  key: Key('credit-product-${widget.products[i].code}'),
                  label: Text(
                    '${widget.products[i].name} '
                    '${formatPercent(widget.products[i].tea ?? widget.products[i].tcea)}',
                  ),
                  selected: i == _productIndex,
                  onSelected: (_) => _onProductSelected(i),
                ),
            ],
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'TEA referencial del producto elegido.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ],
      ),
    );
  }

  Widget _amountCard() {
    final product = _product;
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            key: const Key('credit-amount-label'),
            'Monto: ${formatMinor(_amountMinor, 'PEN')}',
            style: AppTypography.bodyLg.copyWith(
              color: AppColors.onSurface,
              fontWeight: FontWeight.w600,
            ),
          ),
          Slider(
            key: const Key('credit-amount-slider'),
            value: _amountMinor.toDouble().clamp(
              product.minAmountMinor.toDouble(),
              product.maxAmountMinor.toDouble(),
            ),
            min: product.minAmountMinor.toDouble(),
            max: product.maxAmountMinor.toDouble(),
            divisions: _amountDivisions,
            label: formatMinor(_amountMinor, 'PEN'),
            onChanged: _onAmountChanged,
          ),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                formatMinor(product.minAmountMinor, 'PEN'),
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              Text(
                formatMinor(product.maxAmountMinor, 'PEN'),
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _termCard() {
    final terms = termsFor(_product);
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Plazo: $_termMonths meses',
            style: AppTypography.bodyLg.copyWith(
              color: AppColors.onSurface,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final term in terms)
                ChoiceChip(
                  key: Key('credit-term-$term'),
                  label: Text('$term'),
                  selected: term == _termMonths,
                  onSelected: (_) => _onTermSelected(term),
                ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _insuranceCard() {
    return AppCard(
      padding: const EdgeInsets.symmetric(
        horizontal: AppSpacing.containerPadding,
        vertical: AppSpacing.stackSm,
      ),
      child: SwitchListTile(
        key: const Key('credit-insurance-switch'),
        contentPadding: EdgeInsets.zero,
        title: Text(
          'Seguro de desgravamen',
          style: AppTypography.bodyLg.copyWith(
            color: AppColors.onSurface,
            fontWeight: FontWeight.w600,
          ),
        ),
        subtitle: Text(
          'Recalcula la cuota. Opcional.',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
        value: _insurance,
        onChanged: _onInsuranceChanged,
      ),
    );
  }

  /// Card de cuota: valores TAL CUAL responde el servidor + skeleton/error.
  Widget _quotaCard() {
    final result = _result;
    final error = _simError;
    Widget body;
    if (_simulating && result == null) {
      body = const _QuotaSkeleton(key: Key('credit-quota-loading'));
    } else if (error != null && result == null) {
      body = _QuotaError(
        message: error is ApiException
            ? creditUserMessage(error)
            : 'Ocurrió un error al consultar el servidor.',
        onRetry: _simulateNow,
      );
    } else if (result != null) {
      body = _QuotaContent(result: result);
    } else {
      body = const _QuotaSkeleton(key: Key('credit-quota-loading'));
    }
    return Container(
      key: const Key('credit-quota-card'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.secondaryContainer,
        borderRadius: BorderRadius.circular(AppRadii.lg),
        boxShadow: AppShadows.cardList,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (_simulating && result != null)
            const Padding(
              padding: EdgeInsets.only(bottom: 8),
              child: LinearProgressIndicator(),
            ),
          body,
        ],
      ),
    );
  }

  Widget _scheduleCard(SimulationResult result) {
    final preview = result.schedule.take(3).toList();
    return AppCard(
      key: const Key('credit-schedule'),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Cronograma',
            style: AppTypography.labelSm.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          const _ScheduleHeader(),
          for (final row in preview) _ScheduleRowView(row: row),
          const SizedBox(height: AppSpacing.stackSm),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              key: const Key('credit-full-schedule'),
              onPressed: () => _showFullSchedule(result),
              child: Text(
                'Ver tabla completa (${result.schedule.length} cuotas)',
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
}

/// Contenido de la card de cuota (valores del servidor, sin recalcular).
class _QuotaContent extends StatelessWidget {
  const _QuotaContent({required this.result});

  final SimulationResult result;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          'CUOTA MENSUAL (del servidor)',
          style: AppTypography.labelSm.copyWith(
            fontWeight: FontWeight.w600,
            color: AppColors.onSecondaryContainer,
          ),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        Text(
          key: const Key('credit-monthly-payment'),
          formatMinor(result.paymentMinor, 'PEN'),
          style: AppTypography.displayLg.copyWith(
            fontSize: 36,
            fontWeight: FontWeight.bold,
            color: AppColors.onSecondaryFixed,
          ),
        ),
        const SizedBox(height: AppSpacing.stackSm),
        Text(
          key: const Key('credit-rates'),
          'TEA ${formatPercent(result.tea)} • '
          'TCEA ${formatPercent(result.tcea)} • Francesa',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.onSecondaryFixed,
          ),
        ),
      ],
    );
  }
}

/// Skeleton de la card de cuota durante la peticion al servidor.
class _QuotaSkeleton extends StatelessWidget {
  const _QuotaSkeleton({super.key});

  @override
  Widget build(BuildContext context) {
    return const Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 24,
          height: 24,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
        SizedBox(height: 8),
        Text('Calculando cuota...'),
      ],
    );
  }
}

/// Error de simulacion (422/404/red) con reintento accionable.
class _QuotaError extends StatelessWidget {
  const _QuotaError({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          key: const Key('credit-quota-error'),
          message,
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.onSecondaryFixed,
          ),
        ),
        const SizedBox(height: 8),
        FilledButton(
          onPressed: onRetry,
          child: const Text('Reintentar'),
        ),
      ],
    );
  }
}

/// Cabecera de la tabla del cronograma (`N · Capital · Interés · Saldo`).
class _ScheduleHeader extends StatelessWidget {
  const _ScheduleHeader();

  @override
  Widget build(BuildContext context) {
    final style = AppTypography.labelSm.copyWith(
      fontWeight: FontWeight.w700,
      color: AppColors.secondaryText,
    );
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: [
          SizedBox(width: 36, child: Text('N', style: style)),
          Expanded(child: Text('Capital', style: style)),
          Expanded(child: Text('Interés', style: style)),
          Expanded(
            child: Text('Saldo', style: style, textAlign: TextAlign.end),
          ),
        ],
      ),
    );
  }
}

/// Fila del cronograma (montos del servidor, formato `S/ 9,999.99`).
class _ScheduleRowView extends StatelessWidget {
  const _ScheduleRowView({required this.row});

  final ScheduleRow row;

  @override
  Widget build(BuildContext context) {
    final style = AppTypography.bodyMd.copyWith(
      color: AppColors.onSurface,
    );
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: [
          SizedBox(
            width: 36,
            child: Text('${row.installmentNo}', style: style),
          ),
          Expanded(
            child: Text(
              formatMinor(row.principalMinor, 'PEN'),
              style: style,
            ),
          ),
          Expanded(
            child: Text(
              formatMinor(row.interestMinor, 'PEN'),
              style: style,
            ),
          ),
          Expanded(
            child: Text(
              formatMinor(row.balanceMinor, 'PEN'),
              style: style,
              textAlign: TextAlign.end,
            ),
          ),
        ],
      ),
    );
  }
}

/// Tabla completa del cronograma (todas las cuotas del servidor).
class _FullScheduleSheet extends StatelessWidget {
  const _FullScheduleSheet({required this.result});

  final SimulationResult result;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: SizedBox(
        height: MediaQuery.of(context).size.height * 0.8,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'Cronograma completo (${result.schedule.length} cuotas)',
                style: AppTypography.titleMd.copyWith(
                  color: AppColors.primary,
                ),
              ),
              const SizedBox(height: 8),
              const _ScheduleHeader(),
              const Divider(height: 1),
              Expanded(
                child: ListView.builder(
                  itemCount: result.schedule.length,
                  itemBuilder: (context, index) => _ScheduleRowView(
                    row: result.schedule[index],
                  ),
                ),
              ),
              const SizedBox(height: 8),
              SizedBox(
                width: double.infinity,
                child: FilledButton(
                  onPressed: () => Navigator.of(context).pop(),
                  child: const Text('Cerrar'),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
