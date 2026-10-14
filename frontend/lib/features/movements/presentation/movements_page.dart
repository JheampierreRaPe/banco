import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/error_view.dart';
import '../data/movements_service.dart';
import '../models/movement.dart';

/// Historial de movimientos (E2-T24; mockups `Historial 1/2/3/4`).
///
/// - Paginado `page`/`page_size` con `Cargar más` + pull-to-refresh.
/// - Filtros del servidor: fecha (`date_from`/`date_to`), tipo y estado;
///   la caja de texto filtra en memoria solo la página cargada (E2-T24 no
///   expone `q`, asi que el texto nunca viaja al servidor).
/// - Datos siempre enmascarados (mascaras del backend; jamas PII en claro).
/// - 4 estados de UI: skeleton (carga), vacio, error + reintento, exito.
///
/// Fondo `#F5F3EE`, tarjetas blancas, Inter (tema `Eucalipto y Ocre`).
class MovementsPage extends StatefulWidget {
  const MovementsPage({super.key, this.service});

  final MovementsServiceBase? service;

  @override
  State<MovementsPage> createState() => _MovementsPageState();
}

class _MovementsPageState extends State<MovementsPage> {
  static const int _pageSize = 10;

  final _searchController = TextEditingController();
  Timer? _debounce;

  MovementFilters _filters = const MovementFilters();
  MovementDatePreset _datePreset = MovementDatePreset.all;
  String? _typeValue;
  String? _statusValue;

  final List<Movement> _items = [];
  int _page = 1;
  int _total = 0;
  bool _loading = true;
  bool _loadingMore = false;
  ApiException? _error;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _searchController.dispose();
    super.dispose();
  }

  MovementsServiceBase? get _service => widget.service;

  Future<void> _reload() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    final service = _service;
    if (service == null) {
      setState(() {
        _loading = false;
        _error = ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de movimientos no configurado.',
        );
      });
      return;
    }
    try {
      final result = await service.list(
        page: 1,
        pageSize: _pageSize,
        filters: _filters,
      );
      if (!mounted) return;
      setState(() {
        _items
          ..clear()
          ..addAll(result.items);
        _page = result.page;
        _total = result.total;
        _loading = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e;
      });
    } on FormatException {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = ApiException(
          code: 'SERVER_ERROR',
          message: 'Ocurrió un error al consultar el servidor.',
        );
      });
    }
  }

  Future<void> _loadMore() async {
    final service = _service;
    if (service == null || _loadingMore) return;
    if (_items.length >= _total) return;
    setState(() => _loadingMore = true);
    try {
      final result = await service.list(
        page: _page + 1,
        pageSize: _pageSize,
        filters: _filters,
      );
      if (!mounted) return;
      setState(() {
        _items.addAll(result.items);
        _page = result.page;
        _total = result.total;
        _loadingMore = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _loadingMore = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(movementUserMessage(e))),
      );
    }
  }

  void _onSearchChanged(String value) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 350), () {
      setState(() {
        _filters = _filters.copyWith(query: value);
      });
    });
  }

  /// Cabecera con datos reales (auditoria M8): usa la cuenta/moneda del
  /// primer movimiento cargado; sin datos, texto neutro (jamas inventado).
  String _stripTitle() {
    if (_items.isEmpty) return 'Mis movimientos';
    final first = _items.first;
    final masked = (first.sourceMasked ?? '').trim().isNotEmpty
        ? first.sourceMasked!.trim()
        : (first.targetMasked ?? '').trim();
    if (masked.isEmpty) return 'Mis movimientos';
    return 'Cuenta $masked';
  }

  /// Moneda del primer movimiento (`Soles (PEN)`/`Dólares (USD)`); neutro
  /// sin datos. Solo exhibe lo que el servidor ya devolvio enmascarado.
  String _stripSubtitle() {
    if (_items.isEmpty) return 'Todas las cuentas';
    switch (_items.first.currency.toUpperCase()) {
      case 'USD':
        return 'Dólares (USD)';
      case 'PEN':
        return 'Soles (PEN)';
      default:
        return _items.first.currency.toUpperCase();
    }
  }

  void _clearFilters() {
    _searchController.clear();
    setState(() {
      _filters = const MovementFilters();
      _datePreset = MovementDatePreset.all;
      _typeValue = null;
      _statusValue = null;
    });
    _reload();
  }

  void _applyServerFilters() {
    final range = dateRangeForPreset(_datePreset);
    setState(() {
      _filters = MovementFilters(
        dateFrom: range.from,
        dateTo: range.to,
        type: _typeValue,
        status: _statusValue,
        query: _filters.query,
      );
    });
    _reload();
  }

  /// Items visibles tras el filtro local de texto (alias/DNI/num. op).
  List<Movement> get _visible {
    final q = _filters.query.trim().toLowerCase();
    if (q.isEmpty) return _items;
    return _items.where((m) {
      final hay = '${m.title} ${m.operationLabel} ${m.externalRef ?? ''} '
              '${m.sourceMasked ?? ''} ${m.targetMasked ?? ''} '
              '${typeLabel(m.type)} ${statusLabel(m.status)}'
          .toLowerCase();
      return hay.contains(q);
    }).toList();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surfaceContainerLow,
      appBar: AppBar(
        backgroundColor: AppColors.surfaceContainerLow,
        title: const Text('Historial de movimientos'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => context.canPop() ? context.pop() : null,
        ),
        // H6: sin atajo a `/profile` desde el historial (destino incoherente
        // en contexto de movimientos): la ayuda es el reintento/limpiar
        // filtros de la pantalla, no el perfil.
      ),
      body: RefreshIndicator(
        onRefresh: _reload,
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
          children: [
            _AccountStrip(
              title: _stripTitle(),
              subtitle: _stripSubtitle(),
            ),
            const SizedBox(height: 12),
            _SearchBox(
              controller: _searchController,
              onChanged: _onSearchChanged,
            ),
            const SizedBox(height: 12),
            _FilterChips(
              datePreset: _datePreset,
              typeValue: _typeValue,
              statusValue: _statusValue,
              onDate: _pickDate,
              onType: _pickType,
              onStatus: _pickStatus,
            ),
            const SizedBox(height: 12),
            if (_loading)
              const _MovementsSkeleton()
            else if (_error != null)
              _MovementsError(
                error: _error!,
                onRetry: _reload,
                // Auditoria M6: sin promesas falsas. No existe endpoint de
                // "reportar error": el boton conserva ayuda honesta (contactar
                // a soporte) en vez de simular un envio con aviso por correo.
                onReport: () => ScaffoldMessenger.of(context).showSnackBar(
                  const SnackBar(
                    content: Text(
                      'Si necesitas ayuda con este error, '
                      'contacta a Soporte CuyCash 24/7.',
                    ),
                  ),
                ),
              )
            else
              _ResultBody(
                visible: _visible,
                total: _total,
                loaded: _items.length,
                hasQueryOrFilters:
                    _filters.query.trim().isNotEmpty || !_filters.isEmpty,
                hasMore: _items.length < _total,
                loadingMore: _loadingMore,
                onLoadMore: _loadMore,
                onClear: _clearFilters,
                onHelp: () => ScaffoldMessenger.of(context).showSnackBar(
                  const SnackBar(
                    content: Text(
                      'Busca por alias, DNI o número de operación. '
                      'Ajusta fecha, tipo y estado con los filtros.',
                    ),
                  ),
                ),
                onOpen: (m) => context.push('/movements/${m.transactionId}'),
              ),
          ],
        ),
      ),
    );
  }

  Future<void> _pickDate() async {
    final selected = await showModalBottomSheet<MovementDatePreset>(
      context: context,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            for (final preset in MovementDatePreset.values)
              ListTile(
                key: Key('movements-date-${preset.name}'),
                title: Text(_dateLabel(preset)),
                trailing: preset == _datePreset
                    ? const Icon(Icons.check, color: AppColors.primary)
                    : null,
                onTap: () => Navigator.of(context).pop(preset),
              ),
          ],
        ),
      ),
    );
    if (selected == null) return;
    setState(() => _datePreset = selected);
    _applyServerFilters();
  }

  Future<void> _pickType() async {
    const options = <String?, String>{
      null: 'Todos los tipos',
      'OWN_TRANSFER': 'Entre mis cuentas',
      'THIRD_PARTY_TRANSFER': 'Transferencias',
      'INTERBANK_TRANSFER': 'Pagos CCE',
    };
    final selected = await showModalBottomSheet<String?>(
      context: context,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            for (final entry in options.entries)
              ListTile(
                key: Key('movements-type-${entry.key ?? 'all'}'),
                title: Text(entry.value),
                trailing: entry.key == _typeValue
                    ? const Icon(Icons.check, color: AppColors.primary)
                    : null,
                onTap: () => Navigator.of(context).pop(entry.key),
              ),
          ],
        ),
      ),
    );
    // `null` es una seleccion valida ("Todos"); el dismiss tambien da
    // `null`: re-aplicar es inocuo.
    setState(() => _typeValue = selected);
    _applyServerFilters();
  }

  Future<void> _pickStatus() async {
    const options = <String?, String>{
      null: 'Todos los estados',
      'SETTLED': 'Liquidados',
      'POSTED': 'Publicados',
      'CONCILIATED': 'Conciliados',
      'PENDING': 'Pendientes',
      'REVERSED': 'Reversados',
    };
    final selected = await showModalBottomSheet<String?>(
      context: context,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            for (final entry in options.entries)
              ListTile(
                key: Key('movements-status-${entry.key ?? 'all'}'),
                title: Text(entry.value),
                trailing: entry.key == _statusValue
                    ? const Icon(Icons.check, color: AppColors.primary)
                    : null,
                onTap: () => Navigator.of(context).pop(entry.key),
              ),
          ],
        ),
      ),
    );
    setState(() => _statusValue = selected);
    _applyServerFilters();
  }
}

String _dateLabel(MovementDatePreset preset) {
  switch (preset) {
    case MovementDatePreset.all:
      return 'Todas las fechas';
    case MovementDatePreset.today:
      return 'Hoy';
    case MovementDatePreset.last7:
      return 'Últimos 7 días';
    case MovementDatePreset.last30:
      return 'Últimos 30 días';
  }
}

/// Franja de cuenta + boton Extracto (mockup Historial 1, `AccountStrip`).
///
/// Auditoria M6/M8: el historial global (`GET /transactions`) no tiene
/// cuenta seleccionada ni rango, asi que no puede llamar a
/// `AccountsService.exportMovements` (exige `accountId` + `dateFrom`/`dateTo`
/// de `GET /accounts/{id}/movements/export`). El boton NO promete envio por
/// correo: indica la descarga real (detalle de cuenta -> Exportar CSV).
/// Titulo/subtitulo con datos reales del primer movimiento o texto neutro.
class _AccountStrip extends StatelessWidget {
  const _AccountStrip({required this.title, required this.subtitle});

  final String title;
  final String subtitle;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          width: 32,
          height: 32,
          decoration: const BoxDecoration(
            color: AppColors.successContainer,
            shape: BoxShape.circle,
          ),
          child: const Icon(
            Icons.account_balance_wallet_outlined,
            size: 18,
            color: AppColors.primary,
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                title,
                style: const TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                  color: AppColors.primary,
                ),
              ),
              Text(
                subtitle,
                style: const TextStyle(fontSize: 11, color: AppColors.outline),
              ),
            ],
          ),
        ),
        FilledButton.tonal(
          key: const Key('movements-extract'),
          onPressed: () => ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text(
                'El extracto se descarga desde el detalle de tu cuenta '
                '(Exportar CSV).',
              ),
            ),
          ),
          style: FilledButton.styleFrom(
            backgroundColor: AppColors.surfaceContainerHighest,
            foregroundColor: AppColors.primary,
            minimumSize: const Size(0, 30),
            padding: const EdgeInsets.symmetric(horizontal: 12),
            textStyle: const TextStyle(
              fontSize: 12,
              fontWeight: FontWeight.w500,
            ),
          ),
          child: const Text('Extracto'),
        ),
      ],
    );
  }
}

/// Buscador (mockup `Search`): filtra en memoria la pagina cargada.
class _SearchBox extends StatelessWidget {
  const _SearchBox({required this.controller, required this.onChanged});

  final TextEditingController controller;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 48,
      padding: const EdgeInsets.symmetric(horizontal: 14),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLowest,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        children: [
          const Icon(Icons.search, size: 20, color: AppColors.outline),
          const SizedBox(width: 8),
          Expanded(
            child: TextField(
              key: const Key('movements-search'),
              controller: controller,
              onChanged: onChanged,
              textInputAction: TextInputAction.search,
              decoration: const InputDecoration(
                hintText: 'Buscar por alias, DNI o número de op...',
                border: InputBorder.none,
                filled: false,
                contentPadding: EdgeInsets.zero,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Chips de filtro fecha/tipo/estado (mockup `Chips`, scroll horizontal).
class _FilterChips extends StatelessWidget {
  const _FilterChips({
    required this.datePreset,
    required this.typeValue,
    required this.statusValue,
    required this.onDate,
    required this.onType,
    required this.onStatus,
  });

  final MovementDatePreset datePreset;
  final String? typeValue;
  final String? statusValue;
  final VoidCallback onDate;
  final VoidCallback onType;
  final VoidCallback onStatus;

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      scrollDirection: Axis.horizontal,
      child: Row(
        children: [
          _Chip(
            key: const Key('movements-filter-date'),
            label: _dateLabel(datePreset),
            selected: datePreset != MovementDatePreset.all,
            icon: Icons.calendar_today_outlined,
            trailing: Icons.keyboard_arrow_down,
            onTap: onDate,
          ),
          const SizedBox(width: 8),
          _Chip(
            key: const Key('movements-filter-type'),
            label: _typeShortLabel(typeValue),
            selected: typeValue != null,
            icon: Icons.swap_horiz,
            onTap: onType,
          ),
          const SizedBox(width: 8),
          _Chip(
            key: const Key('movements-filter-state'),
            label: _statusShortLabel(statusValue),
            selected: statusValue != null,
            icon: Icons.account_balance_wallet_outlined,
            onTap: onStatus,
          ),
        ],
      ),
    );
  }
}

String _typeShortLabel(String? value) {
  switch (value) {
    case 'OWN_TRANSFER':
      return 'Entre mis cuentas';
    case 'THIRD_PARTY_TRANSFER':
      return 'Transferencias';
    case 'INTERBANK_TRANSFER':
      return 'Pagos CCE';
    default:
      return 'Tipo: todos';
  }
}

String _statusShortLabel(String? value) {
  switch (value) {
    case 'SETTLED':
      return 'Liquidados';
    case 'POSTED':
      return 'Publicados';
    case 'CONCILIATED':
      return 'Conciliados';
    case 'PENDING':
      return 'Pendientes';
    case 'REVERSED':
      return 'Reversados';
    default:
      return 'Estado: todos';
  }
}

class _Chip extends StatelessWidget {
  const _Chip({
    super.key,
    required this.label,
    required this.selected,
    required this.icon,
    required this.onTap,
    this.trailing,
  });

  final String label;
  final bool selected;
  final IconData icon;
  final VoidCallback onTap;
  final IconData? trailing;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        height: 32,
        padding: const EdgeInsets.symmetric(horizontal: 12),
        decoration: BoxDecoration(
          color: selected
              ? AppColors.primary
              : AppColors.surfaceContainerLowest,
          borderRadius: BorderRadius.circular(16),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              icon,
              size: 15,
              color: selected ? Colors.white : AppColors.onSurfaceVariant,
            ),
            const SizedBox(width: 6),
            Text(
              label,
              style: TextStyle(
                fontSize: 12,
                fontWeight: FontWeight.w500,
                color: selected ? Colors.white : AppColors.onSurfaceVariant,
              ),
            ),
            if (trailing != null) ...[
              const SizedBox(width: 2),
              Icon(
                trailing,
                size: 14,
                color: selected ? Colors.white : AppColors.onSurfaceVariant,
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// Cuerpo del resultado: conteo + grupos por dia + paginacion.
class _ResultBody extends StatelessWidget {
  const _ResultBody({
    required this.visible,
    required this.total,
    required this.loaded,
    required this.hasQueryOrFilters,
    required this.hasMore,
    required this.loadingMore,
    required this.onLoadMore,
    required this.onClear,
    required this.onHelp,
    required this.onOpen,
  });

  final List<Movement> visible;
  final int total;
  final int loaded;
  final bool hasQueryOrFilters;
  final bool hasMore;
  final bool loadingMore;
  final VoidCallback onLoadMore;
  final VoidCallback onClear;
  final VoidCallback onHelp;
  final ValueChanged<Movement> onOpen;

  @override
  Widget build(BuildContext context) {
    if (visible.isEmpty) {
      return _MovementsEmpty(onClear: onClear, onHelp: onHelp);
    }
    final groups = _groupByDay(visible);
    final showing = loaded > total ? total : loaded;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Text(
              '$total movimientos encontrados',
              style: const TextStyle(
                fontSize: 12,
                color: AppColors.onSurfaceVariant,
              ),
            ),
            Text(
              'Mostrando 1–$showing',
              style: const TextStyle(fontSize: 11, color: AppColors.outline),
            ),
          ],
        ),
        const SizedBox(height: 12),
        for (final group in groups) ...[
          _DayGroup(
            group: group,
            onOpen: onOpen,
          ),
          const SizedBox(height: 12),
        ],
        if (hasMore)
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('movements-load-more'),
              onPressed: loadingMore ? null : onLoadMore,
              child: loadingMore
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Text('Cargar más movimientos'),
            ),
          ),
        const SizedBox(height: 8),
        const Text(
          'Los saldos se actualizan al liquidar cada operación.',
          style: TextStyle(fontSize: 11, color: AppColors.outline),
        ),
      ],
    );
  }
}

class _DayItems {
  _DayItems(this.title, this.items);

  final String title;
  final List<Movement> items;
}

List<_DayItems> _groupByDay(List<Movement> items) {
  final ordered = List<Movement>.from(items)
    ..sort((a, b) {
      final da = a.createdAt;
      final db = b.createdAt;
      if (da == null && db == null) return 0;
      if (da == null) return 1;
      if (db == null) return -1;
      return db.compareTo(da);
    });
  final groups = <String, List<Movement>>{};
  for (final m in ordered) {
    final key = _dayKey(m.createdAt);
    groups.putIfAbsent(key, () => []).add(m);
  }
  return groups.entries.map((e) => _DayItems(e.key, e.value)).toList();
}

String _dayKey(DateTime? date) {
  if (date == null) return 'SIN FECHA';
  const months = [
    'ENE', 'FEB', 'MAR', 'ABR', 'MAY', 'JUN',
    'JUL', 'AGO', 'SEP', 'OCT', 'NOV', 'DIC',
  ];
  final now = DateTime.now();
  final today = DateTime(now.year, now.month, now.day);
  final day = DateTime(date.year, date.month, date.day);
  final label =
      '${date.day.toString().padLeft(2, '0')} ${months[date.month - 1]} ${date.year}';
  if (day == today) return 'HOY · $label';
  if (day == today.subtract(const Duration(days: 1))) return 'AYER · $label';
  return label;
}

/// Grupo del dia: cabecera + tarjetas (mockup `Group-Hoy`/`Group-Ayer`).
class _DayGroup extends StatelessWidget {
  const _DayGroup({required this.group, required this.onOpen});

  final _DayItems group;
  final ValueChanged<Movement> onOpen;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Expanded(
              child: Text(
                group.title,
                style: const TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.w600,
                  color: AppColors.outline,
                ),
              ),
            ),
            Text(
              group.items.length == 1
                  ? '1 transacción'
                  : '${group.items.length} transacciones',
              style: const TextStyle(fontSize: 11, color: AppColors.outline),
            ),
          ],
        ),
        const SizedBox(height: 8),
        for (final m in group.items) ...[
          _MovementCard(movement: m, onOpen: () => onOpen(m)),
          const SizedBox(height: 8),
        ],
      ],
    );
  }
}

/// Tarjeta de movimiento (mockup Historial 1: avatar + alias + badge + monto).
class _MovementCard extends StatelessWidget {
  const _MovementCard({required this.movement, required this.onOpen});

  final Movement movement;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context) {
    final m = movement;
    final upper = m.status.toUpperCase();
    final inbound = m.amountMinor > 0;
    final icon = _iconFor(m);
    final avatarBg = _avatarBg(upper, inbound);
    final iconColor = _iconColor(upper, inbound);
    final amountColor =
        inbound ? AppColors.success : AppColors.onSurface;
    final sign = inbound ? '+' : '-';
    return GestureDetector(
      key: Key('movement-card-${m.transactionId}'),
      onTap: onOpen,
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: AppColors.surfaceContainerLowest,
          borderRadius: BorderRadius.circular(14),
        ),
        child: Row(
          children: [
            Container(
              width: 44,
              height: 44,
              decoration: BoxDecoration(
                color: avatarBg,
                shape: BoxShape.circle,
              ),
              child: Icon(icon, size: 20, color: iconColor),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Flexible(
                        child: Text(
                          m.title,
                          style: AppTypography.titleMd.copyWith(fontSize: 14),
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                      const SizedBox(width: 6),
                      _StatusBadge(status: m.status),
                    ],
                  ),
                  const SizedBox(height: 2),
                  Text(
                    '${m.sourceMasked ?? typeLabel(m.type)} · ${typeLabel(m.type)}',
                    style: const TextStyle(
                      fontSize: 12,
                      color: AppColors.onSurfaceVariant,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                  const SizedBox(height: 2),
                  Text(
                    m.operationLabel,
                    style: const TextStyle(
                      fontSize: 10,
                      color: AppColors.outline,
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            Column(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Text(
                  '$sign ${formatMoneyMinor(m.amountMinor.abs(), m.currency)}',
                  style: TextStyle(
                    fontSize: 14,
                    fontWeight: FontWeight.bold,
                    color: amountColor,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  _subLabel(upper),
                  style: TextStyle(
                    fontSize: 10,
                    fontWeight: FontWeight.w600,
                    color: _subColor(upper, inbound),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

IconData _iconFor(Movement m) {
  switch (m.status.toUpperCase()) {
    case 'REVERSED':
    case 'REJECTED':
    case 'FAILED':
      return Icons.history;
    case 'PENDING':
    case 'PROCESSING':
    case 'PENDING_AUTHORIZATION':
      return Icons.schedule;
    default:
      return m.amountMinor > 0 ? Icons.south_west : Icons.north_east;
  }
}

Color _avatarBg(String upper, bool inbound) {
  switch (upper) {
    case 'REVERSED':
    case 'REJECTED':
    case 'FAILED':
      return AppColors.errorContainer;
    case 'PENDING':
    case 'PROCESSING':
    case 'PENDING_AUTHORIZATION':
      return AppColors.warningContainer;
    case 'SETTLED':
    case 'CONCILIATED':
      return inbound
          ? const Color(0xFFDDF0E2)
          : AppColors.surfaceContainer;
    default:
      return AppColors.surfaceContainer;
  }
}

Color _iconColor(String upper, bool inbound) {
  switch (upper) {
    case 'REVERSED':
    case 'REJECTED':
    case 'FAILED':
      return AppColors.error;
    case 'PENDING':
    case 'PROCESSING':
    case 'PENDING_AUTHORIZATION':
      return AppColors.onWarningContainer;
    case 'SETTLED':
    case 'CONCILIATED':
      return inbound ? AppColors.success : AppColors.primary;
    default:
      return AppColors.primary;
  }
}

String _subLabel(String upper) {
  switch (upper) {
    case 'SETTLED':
    case 'CONCILIATED':
      return 'Conciliado';
    case 'PENDING':
    case 'PROCESSING':
      return 'En validación';
    case 'REVERSED':
      return 'Reintegrado';
    case 'REJECTED':
    case 'FAILED':
      return 'No debitado';
    default:
      return 'Sin comisión';
  }
}

Color _subColor(String upper, bool inbound) {
  switch (upper) {
    case 'REVERSED':
    case 'REJECTED':
    case 'FAILED':
      return AppColors.error;
    case 'PENDING':
    case 'PROCESSING':
      return AppColors.secondary;
    case 'SETTLED':
    case 'CONCILIATED':
      return inbound ? AppColors.secondary : AppColors.outline;
    default:
      return AppColors.outline;
  }
}

/// Badge de estado (mockup: `POSTED`/`SETTLED`/`PROCESANDO`/`REVERSADO`).
class _StatusBadge extends StatelessWidget {
  const _StatusBadge({required this.status});

  final String status;

  @override
  Widget build(BuildContext context) {
    final upper = status.toUpperCase();
    late final Color bg;
    late final Color fg;
    switch (upper) {
      case 'POSTED':
        bg = AppColors.successContainer;
        fg = AppColors.primary;
      case 'SETTLED':
      case 'CONCILIATED':
        bg = const Color(0xFFDDF0E2);
        fg = AppColors.success;
      case 'PENDING':
      case 'PROCESSING':
      case 'PENDING_AUTHORIZATION':
        bg = AppColors.warningContainer;
        fg = AppColors.onWarningContainer;
      case 'REVERSED':
      case 'REJECTED':
      case 'FAILED':
        bg = AppColors.errorContainer;
        fg = AppColors.error;
      default:
        bg = AppColors.surfaceContainerHighest;
        fg = AppColors.onSurfaceVariant;
    }
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(
        statusLabel(status),
        style: TextStyle(fontSize: 9, fontWeight: FontWeight.w600, color: fg),
      ),
    );
  }
}

/// Skeleton de carga (mockup `Historial 2 - Skeleton`: tarjetas 72px).
class _MovementsSkeleton extends StatelessWidget {
  const _MovementsSkeleton();

  @override
  Widget build(BuildContext context) {
    return Column(
      key: const Key('movements-skeleton'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Container(
          height: 12,
          width: 96,
          decoration: BoxDecoration(
            color: AppColors.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(6),
          ),
        ),
        const SizedBox(height: 12),
        for (var i = 0; i < 4; i++) ...[
          Container(
            height: 72,
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: AppColors.surfaceContainerLowest,
              borderRadius: BorderRadius.circular(14),
            ),
            child: Row(
              children: [
                Container(
                  width: 44,
                  height: 44,
                  decoration: const BoxDecoration(
                    color: AppColors.surfaceContainerHigh,
                    shape: BoxShape.circle,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Container(
                        height: 14,
                        width: 144,
                        decoration: BoxDecoration(
                          color: AppColors.surfaceContainerHighest,
                          borderRadius: BorderRadius.circular(7),
                        ),
                      ),
                      const SizedBox(height: 8),
                      Container(
                        height: 10,
                        width: 192,
                        decoration: BoxDecoration(
                          color: AppColors.surfaceContainerHigh,
                          borderRadius: BorderRadius.circular(5),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
        ],
      ],
    );
  }
}

/// Estado vacio (mockup `Historial 3 - Vacio`).
class _MovementsEmpty extends StatelessWidget {
  const _MovementsEmpty({required this.onClear, required this.onHelp});

  final VoidCallback onClear;
  final VoidCallback onHelp;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        const SizedBox(height: 28),
        Container(
          width: 80,
          height: 80,
          decoration: const BoxDecoration(
            color: AppColors.surfaceContainerHigh,
            shape: BoxShape.circle,
          ),
          child: const Icon(
            Icons.account_balance_wallet_outlined,
            size: 40,
            color: AppColors.secondary,
          ),
        ),
        const SizedBox(height: 12),
        const Text(
          'No encontramos movimientos',
          textAlign: TextAlign.center,
          style: TextStyle(
            fontSize: 16,
            fontWeight: FontWeight.w600,
            color: AppColors.primary,
          ),
        ),
        const SizedBox(height: 8),
        const Text(
          'No hay registros que coincidan con los filtros aplicados o tu '
          'término de búsqueda en este rango de fechas.',
          textAlign: TextAlign.center,
          style: TextStyle(fontSize: 12, color: AppColors.onSurfaceVariant),
        ),
        const SizedBox(height: 16),
        SizedBox(
          width: double.infinity,
          height: 44,
          child: FilledButton.icon(
            key: const Key('movements-clear-filters'),
            onPressed: onClear,
            icon: const Icon(Icons.search, size: 16),
            label: const Text(
              'Limpiar filtros y recargar',
              style: TextStyle(fontSize: 12, fontWeight: FontWeight.w600),
            ),
            style: FilledButton.styleFrom(
              backgroundColor: AppColors.primary,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(12),
              ),
            ),
          ),
        ),
        const SizedBox(height: 8),
        // El EmptyView base existe, pero el mockup pide link ocre:
        // se usa TextButton para no duplicar dependencia.
        const _HelpLink(),
      ],
    );
  }
}

class _HelpLink extends StatelessWidget {
  const _HelpLink();

  @override
  Widget build(BuildContext context) {
    return TextButton(
      onPressed: () => ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            'Busca por alias, DNI o número de operación. '
            'Ajusta fecha, tipo y estado con los filtros.',
          ),
        ),
      ),
      child: const Text(
        'Ver ayuda de búsqueda',
        style: TextStyle(
          fontSize: 12,
          fontWeight: FontWeight.w500,
          color: AppColors.secondary,
        ),
      ),
    );
  }
}

/// Estado error (mockup `Historial 4 - Error 500`).
class _MovementsError extends StatelessWidget {
  const _MovementsError({
    required this.error,
    required this.onRetry,
    required this.onReport,
  });

  final ApiException error;
  final VoidCallback onRetry;
  final VoidCallback onReport;

  @override
  Widget build(BuildContext context) {
    final isServer =
        error.code == 'SERVER_ERROR' || error.statusCode != null && error.statusCode! >= 500;
    return Column(
      children: [
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(20),
          decoration: BoxDecoration(
            color: AppColors.errorContainer,
            borderRadius: BorderRadius.circular(14),
          ),
          child: Column(
            children: [
              Container(
                width: 56,
                height: 56,
                decoration: const BoxDecoration(
                  color: AppColors.error,
                  shape: BoxShape.circle,
                ),
                child: const Icon(Icons.close, size: 28, color: Colors.white),
              ),
              const SizedBox(height: 12),
              Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                decoration: BoxDecoration(
                  color: AppColors.error,
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  isServer
                      ? 'ERROR HTTP 500 · E2-T24-ERR'
                      : 'ERROR DE CONSULTA · E2-T24',
                  style: const TextStyle(
                    fontSize: 10,
                    fontWeight: FontWeight.w600,
                    color: Colors.white,
                  ),
                ),
              ),
              const SizedBox(height: 8),
              const Text(
                'Fallo de sincronización bancaria',
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                  color: AppColors.onSurface,
                ),
              ),
              const SizedBox(height: 4),
              const Text(
                'El servicio de mensajería financiera central no respondió a '
                'tiempo. Tus saldos y transacciones continúan seguros.',
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: 12,
                  color: AppColors.onSurfaceVariant,
                ),
              ),
              const SizedBox(height: 12),
              Row(
                children: [
                  Expanded(
                    child: SizedBox(
                      height: 44,
                      child: FilledButton.icon(
                        onPressed: onRetry,
                        icon: const Icon(Icons.refresh, size: 16),
                        label: const Text(
                          'Reintentar consulta',
                          style: TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        style: FilledButton.styleFrom(
                          backgroundColor: AppColors.primary,
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(12),
                          ),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  SizedBox(
                    height: 44,
                    child: FilledButton(
                      key: const Key('movements-report'),
                      onPressed: onReport,
                      style: FilledButton.styleFrom(
                        backgroundColor:
                            AppColors.surfaceContainerLowest,
                        foregroundColor: AppColors.primary,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(12),
                        ),
                      ),
                      child: const Text(
                        'Reportar',
                        style: TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: AppColors.surfaceContainer,
            borderRadius: BorderRadius.circular(12),
          ),
          child: const Row(
            children: [
              Icon(
                Icons.shield_outlined,
                size: 20,
                color: AppColors.secondary,
              ),
              SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '¿Necesitas constancia urgente?',
                      style: TextStyle(
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                        color: AppColors.primary,
                      ),
                    ),
                    SizedBox(height: 4),
                    Text(
                      'Puedes solicitar tu estado de cuenta oficial a tu '
                      'correo desde Soporte CuyCash 24/7.',
                      style: TextStyle(
                        fontSize: 11,
                        color: AppColors.onSurfaceVariant,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 12),
        // Cobertura del widget base (convencion F-T01 §3).
        ErrorView(message: error.message, onRetry: onRetry),
      ],
    );
  }
}
