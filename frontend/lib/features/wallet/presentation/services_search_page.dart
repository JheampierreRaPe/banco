import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../data/services_service.dart';
import '../models/services.dart';
import 'service_receipt_card.dart';

/// Catalogo y busqueda de servicios (E4-T19, HU16 CA-01).
///
/// - Busca empresa por nombre o numero de suministro (`GET
///   /services/catalog` con `q`/`category`; cliente delgado: envia el
///   texto crudo y muestra la lista del servidor, sin filtrar en el
///   cliente mas alla de lo ya devuelto).
/// - Elegir un `biller` abre el pago (`/wallet/services/pay` con el
///   `biller` por `extra`); accesos a recarga y a servicios registrados.
///
/// Sin mockup en `pantallas.fig` (fila pendiente `servicios-recargas.png`,
/// G76; UI desde tokens `docs/20`).
///
/// Estados (docs/20 §7): contenido / carga / vacio / error con Reintentar;
/// mensajes en español; sin logica offline.
class ServicesSearchPage extends StatefulWidget {
  const ServicesSearchPage({super.key, this.service, this.onSelect});

  final ServicesServiceBase? service;

  /// Callback de tests para capturar el `biller` elegido sin navegar.
  final ValueChanged<Biller>? onSelect;

  @override
  State<ServicesSearchPage> createState() => _ServicesSearchPageState();
}

const _serviceCategories = <String>[
  'Todas',
  'ELECTRICIDAD',
  'AGUA',
  'GAS',
  'INTERNET',
  'TELEFONIA',
];

class _ServicesSearchPageState extends State<ServicesSearchPage> {
  final TextEditingController _query = TextEditingController();
  String _category = 'Todas';
  bool _loading = true;
  List<Biller> _billers = [];
  ServiceFailure? _failure;

  @override
  void initState() {
    super.initState();
    _search();
  }

  @override
  void dispose() {
    _query.dispose();
    super.dispose();
  }

  Future<void> _search() async {
    final service = widget.service;
    if (service == null) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _failure = const ServiceFailure(
          message: 'Servicio de servicios no configurado.',
          retry: false,
        );
      });
      return;
    }
    setState(() {
      _loading = true;
      _failure = null;
    });
    try {
      final billers = await service.catalog(
        category: _category == 'Todas' ? null : _category,
        query: _query.text.trim().isEmpty ? null : _query.text.trim(),
      );
      if (!mounted) return;
      setState(() {
        _billers = billers;
        _loading = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _billers = [];
        _loading = false;
        _failure = serviceFailureFor(
          code: e.code,
          fallback: e.message,
        );
      });
    }
  }

  void _select(Biller biller) {
    final onSelect = widget.onSelect;
    if (onSelect != null) {
      onSelect(biller);
      return;
    }
    context.push('/wallet/services/pay', extra: biller);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Servicios y recargas')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: SizedBox(
                    height: 52,
                    child: OutlinedButton.icon(
                      key: const Key('services-go-topup'),
                      onPressed: () => context.push('/wallet/topups'),
                      icon: const Icon(Icons.smartphone_outlined, size: 20),
                      label: const Text('Recargar celular'),
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: SizedBox(
                    height: 52,
                    child: OutlinedButton.icon(
                      key: const Key('services-go-subscriptions'),
                      onPressed: () =>
                          context.push('/wallet/services/subscriptions'),
                      icon: const Icon(Icons.notifications_outlined, size: 20),
                      label: const Text('Mis servicios'),
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            AppCard(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  TextField(
                    key: const Key('services-search-field'),
                    controller: _query,
                    textInputAction: TextInputAction.search,
                    onSubmitted: (_) => _search(),
                    decoration: InputDecoration(
                      labelText: 'Empresa o n.º de suministro',
                      hintText: 'Ej. Luz del Sur o 12345678',
                      filled: true,
                      fillColor: AppColors.surfaceContainerLowest,
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                      suffixIcon: const Icon(Icons.search),
                    ),
                  ),
                  const SizedBox(height: 8),
                  DropdownButtonFormField<String>(
                    key: const Key('services-category-field'),
                    initialValue: _category,
                    decoration: InputDecoration(
                      labelText: 'Categoría',
                      filled: true,
                      fillColor: AppColors.surfaceContainerLowest,
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                    ),
                    items: [
                      for (final c in _serviceCategories)
                        DropdownMenuItem(value: c, child: Text(c)),
                    ],
                    onChanged: (v) =>
                        setState(() => _category = v ?? 'Todas'),
                  ),
                  const SizedBox(height: 8),
                  SizedBox(
                    width: double.infinity,
                    height: 52,
                    child: FilledButton.icon(
                      key: const Key('services-search-button'),
                      onPressed: _loading ? null : _search,
                      icon: const Icon(Icons.search, size: 20),
                      label: const Text(
                        'Buscar',
                        style: TextStyle(
                          fontSize: 16,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(height: 12),
            _buildBody(),
          ],
        ),
      ),
    );
  }

  Widget _buildBody() {
    if (_loading) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 48),
          child: CircularProgressIndicator(
            key: Key('services-search-loading'),
          ),
        ),
      );
    }
    final failure = _failure;
    if (failure != null) {
      return ServiceErrorCard(
        failure: failure,
        onRetry: failure.retry ? _search : null,
        onBack: null,
      );
    }
    if (_billers.isEmpty) {
      return EmptyView(
        key: const Key('services-search-empty'),
        message: 'No encontramos servicios con ese criterio.\n'
            'Prueba con otro nombre o número de suministro.',
        actionLabel: 'Reintentar',
        onAction: _search,
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '${_billers.length} resultado(s) del servidor',
          key: const Key('services-search-count'),
          style: AppTypography.bodyMd.copyWith(
            fontSize: 12,
            color: AppColors.secondaryText,
          ),
        ),
        const SizedBox(height: 8),
        for (final biller in _billers)
          AppCard(
            key: Key('services-biller-${biller.billerId}'),
            margin: const EdgeInsets.only(bottom: 8),
            onTap: () => _select(biller),
            child: Row(
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
                    Icons.bolt_outlined,
                    color: AppColors.primary,
                    size: 24,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(biller.name, style: AppTypography.labelMd),
                      const SizedBox(height: 2),
                      Text(
                        biller.category,
                        style: AppTypography.bodyMd.copyWith(
                          fontSize: 12,
                          color: AppColors.secondaryText,
                        ),
                      ),
                    ],
                  ),
                ),
                const Icon(
                  Icons.chevron_right,
                  color: AppColors.secondaryText,
                ),
              ],
            ),
          ),
      ],
    );
  }
}
