import 'package:flutter/material.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/accounts_service.dart';
import '../models/account.dart';
import '../utils/format.dart';

/// Detalle de cuenta + movimientos (HU05 CA-02/CA-03/CA-04).
///
/// - Cabecera con disponible/retenido/contable diferenciados (mismos colores
///   que el dashboard) y numero enmascarado tal como viene del backend.
/// - Lista de movimientos paginada (`page`/`page_size`) con filtros de fecha
///   (`date_from`/`date_to`) y direccion (`CREDIT`/`DEBIT`).
/// - Boton "Exportar CSV": exige rango de fechas (el backend responde 422 sin
///   rango) y muestra dialogo de guardado + dialogo de exito con el nombre
///   sugerido. El guardado nativo queda pendiente (F-futuro, ver servicio).
class AccountDetailPage extends StatefulWidget {
  const AccountDetailPage({
    super.key,
    required this.accountId,
    this.service,
    this.initialDirection,
    this.initialDateFrom,
    this.initialDateTo,
  });

  final String accountId;
  final AccountsServiceBase? service;

  /// Semillas de filtros (tests y deep-links futuros).
  final String? initialDirection;
  final DateTime? initialDateFrom;
  final DateTime? initialDateTo;

  @override
  State<AccountDetailPage> createState() => _AccountDetailPageState();
}

class _AccountDetailPageState extends State<AccountDetailPage> {
  static const int _pageSize = 20;

  late Future<Account> _detailFuture;
  late String? _direction;
  late DateTime? _dateFrom;
  late DateTime? _dateTo;

  final List<Movement> _movements = [];
  int _page = 1;
  int _total = 0;
  bool _loadingMovements = true;
  bool _loadingMore = false;
  String? _movementsError;
  bool _exporting = false;

  @override
  void initState() {
    super.initState();
    _direction = widget.initialDirection;
    _dateFrom = widget.initialDateFrom;
    _dateTo = widget.initialDateTo;
    _detailFuture = _loadDetail();
    _loadMovements(reset: true);
  }

  AccountsServiceBase? get _service => widget.service;

  Future<Account> _loadDetail() {
    final service = _service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de cuentas no configurado.',
        ),
      );
    }
    return service.getAccountDetail(widget.accountId);
  }

  MovementFilters get _filters => MovementFilters(
        dateFrom: _dateFrom,
        dateTo: _dateTo,
        direction: _direction,
      );

  Future<void> _loadMovements({required bool reset}) async {
    final service = _service;
    if (service == null) {
      if (!mounted) return;
      setState(() {
        _loadingMovements = false;
        _movementsError = 'Servicio de cuentas no configurado.';
      });
      return;
    }
    if (reset) {
      setState(() {
        _loadingMovements = true;
        _movementsError = null;
        _page = 1;
      });
    } else {
      setState(() {
        _loadingMore = true;
        _movementsError = null;
      });
    }
    try {
      final result = await service.getMovements(
        widget.accountId,
        page: reset ? 1 : _page + 1,
        pageSize: _pageSize,
        filters: _filters,
      );
      if (!mounted) return;
      setState(() {
        if (reset) {
          _movements
            ..clear()
            ..addAll(result.items);
          _page = result.page;
        } else {
          _movements.addAll(result.items);
          _page = result.page;
        }
        _total = result.total;
        _loadingMovements = false;
        _loadingMore = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _loadingMovements = false;
        _loadingMore = false;
        _movementsError = e.message;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _loadingMovements = false;
        _loadingMore = false;
        _movementsError = 'Ocurrio un error inesperado. Intentalo mas tarde.';
      });
    }
  }

  bool get _hasMore => _movements.length < _total;

  Future<void> _refreshAll() async {
    setState(() => _detailFuture = _loadDetail());
    await _detailFuture.then((_) {}, onError: (_) {});
    await _loadMovements(reset: true);
  }

  Future<void> _pickDate({required bool from}) async {
    final now = DateTime.now();
    final picked = await showDatePicker(
      context: context,
      initialDate: (from ? _dateFrom : _dateTo) ?? now,
      firstDate: DateTime(2000),
      lastDate: DateTime(now.year + 1, 12, 31),
    );
    if (picked == null) return;
    setState(() {
      if (from) {
        _dateFrom = picked;
      } else {
        _dateTo = picked;
      }
    });
    await _loadMovements(reset: true);
  }

  void _clearFilters() {
    setState(() {
      _direction = null;
      _dateFrom = null;
      _dateTo = null;
    });
    _loadMovements(reset: true);
  }

  String _iso(DateTime d) =>
      '${d.year.toString().padLeft(4, '0')}-'
      '${d.month.toString().padLeft(2, '0')}-'
      '${d.day.toString().padLeft(2, '0')}';

  Future<void> _showExportDialog() async {
    DateTime? from = _dateFrom;
    DateTime? to = _dateTo;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) {
        return StatefulBuilder(
          builder: (context, setDialogState) {
            Future<void> pick(bool isFrom) async {
              final now = DateTime.now();
              final picked = await showDatePicker(
                context: context,
                initialDate: (isFrom ? from : to) ?? now,
                firstDate: DateTime(2000),
                lastDate: DateTime(now.year + 1, 12, 31),
              );
              if (picked == null) return;
              setDialogState(() {
                if (isFrom) {
                  from = picked;
                } else {
                  to = picked;
                }
              });
            }

            final valid = from != null && to != null && !from!.isAfter(to!);
            return AlertDialog(
              title: const Text('Exportar movimientos'),
              content: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text('Formato: CSV'),
                  const SizedBox(height: 12),
                  Row(
                    children: [
                      Expanded(
                        child: OutlinedButton(
                          key: const Key('exportDateFrom'),
                          onPressed: () => pick(true),
                          child: Text(from == null
                              ? 'Desde'
                              : 'Desde: ${_iso(from!)}'),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: OutlinedButton(
                          key: const Key('exportDateTo'),
                          onPressed: () => pick(false),
                          child: Text(
                            to == null ? 'Hasta' : 'Hasta: ${_iso(to!)}',
                          ),
                        ),
                      ),
                    ],
                  ),
                  if (!valid) ...[
                    const SizedBox(height: 8),
                    const Text(
                      'Elige un rango de fechas valido (el backend exige '
                      'rango).',
                      style: TextStyle(color: Colors.red),
                    ),
                  ],
                ],
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(dialogContext).pop(false),
                  child: const Text('Cancelar'),
                ),
                FilledButton(
                  key: const Key('confirmExport'),
                  onPressed: valid
                      ? () => Navigator.of(dialogContext).pop(true)
                      : null,
                  child: const Text('Exportar'),
                ),
              ],
            );
          },
        );
      },
    );
    if (confirmed != true || from == null || to == null) return;
    // Sincroniza el rango elegido con los filtros de la lista.
    setState(() {
      _dateFrom = from;
      _dateTo = to;
    });
    await _loadMovements(reset: true);
    await _runExport(from!, to!);
  }

  Future<void> _runExport(DateTime from, DateTime to) async {
    final service = _service;
    if (service == null || !mounted) return;
    setState(() => _exporting = true);
    try {
      final result = await service.exportMovements(
        widget.accountId,
        format: 'csv',
        dateFrom: from,
        dateTo: to,
      );
      if (!mounted) return;
      setState(() => _exporting = false);
      await showDialog<void>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: const Text('Exportacion lista'),
          content: Text(
            'Archivo: ${result.suggestedFilename}\n'
            'Tamano: ${result.bytes.length} bytes\n\n'
            'Guardado nativo en el dispositivo pendiente (F-futuro): '
            'los bytes ya estan listos para guardar.',
          ),
          actions: [
            FilledButton(
              key: const Key('exportSuccessOk'),
              onPressed: () => Navigator.of(dialogContext).pop(),
              child: const Text('Entendido'),
            ),
          ],
        ),
      );
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _exporting = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('No se pudo exportar: ${e.message}')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Detalle de cuenta')),
      body: FutureBuilder<Account>(
        future: _detailFuture,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingView(message: 'Cargando cuenta...');
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            return ErrorView(
              message: error is ApiException
                  ? error.message
                  : 'Ocurrio un error inesperado. Intentalo mas tarde.',
              onRetry: () =>
                  setState(() => _detailFuture = _loadDetail()),
            );
          }
          final account = snapshot.data!;
          return RefreshIndicator(
            onRefresh: _refreshAll,
            child: ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.all(16),
              children: [
                _BalanceHeader(account: account),
                const SizedBox(height: 12),
                _FiltersBar(
                  direction: _direction,
                  dateFrom: _dateFrom,
                  dateTo: _dateTo,
                  onDirectionChanged: (value) {
                    setState(() => _direction = value);
                    _loadMovements(reset: true);
                  },
                  onPickDate: _pickDate,
                  onClear: _clearFilters,
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: FilledButton.icon(
                        key: const Key('exportButton'),
                        onPressed:
                            _exporting ? null : () => _showExportDialog(),
                        icon: _exporting
                            ? const SizedBox(
                                width: 16,
                                height: 16,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              )
                            : const Icon(Icons.download_outlined),
                        label: const Text('Exportar CSV'),
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Text(
                  'Movimientos ($_total)',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 8),
                _buildMovements(),
              ],
            ),
          );
        },
      ),
    );
  }

  Widget _buildMovements() {
    if (_loadingMovements) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 32),
        child: LoadingView(message: 'Cargando movimientos...'),
      );
    }
    if (_movementsError != null) {
      return ErrorView(
        message: _movementsError!,
        onRetry: () => _loadMovements(reset: true),
      );
    }
    if (_movements.isEmpty) {
      return const EmptyView(
        message: 'No hay movimientos para los filtros elegidos.',
      );
    }
    return Column(
      children: [
        ..._movements.map(_MovementTile.new),
        if (_loadingMore)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 12),
            child: CircularProgressIndicator(),
          ),
        if (_hasMore && !_loadingMore)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: FilledButton.tonal(
              key: const Key('loadMoreButton'),
              onPressed: () => _loadMovements(reset: false),
              child: const Text('Cargar mas'),
            ),
          ),
      ],
    );
  }
}

class _BalanceHeader extends StatelessWidget {
  const _BalanceHeader({required this.account});

  final Account account;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '${account.type} ${account.accountNumberMasked}',
              style: theme.textTheme.titleMedium,
            ),
            if (account.status != null)
              Text(account.status!, style: theme.textTheme.bodySmall),
            const SizedBox(height: 12),
            _AmountLine(
              label: 'Disponible',
              value: formatMinor(account.availableMinor, account.currency),
              color: Colors.green.shade700,
            ),
            _AmountLine(
              label: 'Retenido',
              value: formatMinor(account.heldMinor, account.currency),
              color: Colors.amber.shade800,
            ),
            const Divider(height: 16),
            _AmountLine(
              label: 'Contable',
              value: formatMinor(account.balanceMinor, account.currency),
              color: theme.colorScheme.primary,
              bold: true,
            ),
          ],
        ),
      ),
    );
  }
}

class _AmountLine extends StatelessWidget {
  const _AmountLine({
    required this.label,
    required this.value,
    required this.color,
    this.bold = false,
  });

  final String label;
  final String value;
  final Color color;
  final bool bold;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        children: [
          Expanded(child: Text(label)),
          Text(
            value,
            style: TextStyle(
              color: color,
              fontWeight: bold ? FontWeight.bold : FontWeight.normal,
              fontSize: bold ? 18 : 14,
            ),
          ),
        ],
      ),
    );
  }
}

class _FiltersBar extends StatelessWidget {
  const _FiltersBar({
    required this.direction,
    required this.dateFrom,
    required this.dateTo,
    required this.onDirectionChanged,
    required this.onPickDate,
    required this.onClear,
  });

  final String? direction;
  final DateTime? dateFrom;
  final DateTime? dateTo;
  final ValueChanged<String?> onDirectionChanged;
  final Future<void> Function({required bool from}) onPickDate;
  final VoidCallback onClear;

  String _iso(DateTime d) =>
      '${d.year.toString().padLeft(4, '0')}-'
      '${d.month.toString().padLeft(2, '0')}-'
      '${d.day.toString().padLeft(2, '0')}';

  @override
  Widget build(BuildContext context) {
    final hasFilters =
        direction != null || dateFrom != null || dateTo != null;
    return Column(
      children: [
        DropdownButtonFormField<String?>(
          key: const Key('directionFilter'),
          initialValue: direction,
          decoration: const InputDecoration(
            labelText: 'Direccion',
            border: OutlineInputBorder(),
          ),
          items: const [
            DropdownMenuItem<String?>(value: null, child: Text('Todas')),
            DropdownMenuItem<String?>(
              value: 'CREDIT',
              child: Text('Abonos'),
            ),
            DropdownMenuItem<String?>(
              value: 'DEBIT',
              child: Text('Cargos'),
            ),
          ],
          onChanged: onDirectionChanged,
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: OutlinedButton(
                key: const Key('dateFromButton'),
                onPressed: () => onPickDate(from: true),
                child: Text(
                  dateFrom == null ? 'Desde' : 'Desde: ${_iso(dateFrom!)}',
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: OutlinedButton(
                key: const Key('dateToButton'),
                onPressed: () => onPickDate(from: false),
                child: Text(dateTo == null ? 'Hasta' : 'Hasta: ${_iso(dateTo!)}'),
              ),
            ),
          ],
        ),
        if (hasFilters)
          Align(
            alignment: Alignment.centerRight,
            child: TextButton(
              key: const Key('clearFilters'),
              onPressed: onClear,
              child: const Text('Limpiar filtros'),
            ),
          ),
      ],
    );
  }
}

class _MovementTile extends StatelessWidget {
  const _MovementTile(this.movement);

  final Movement movement;

  @override
  Widget build(BuildContext context) {
    final credit = movement.isCredit;
    final color = credit ? Colors.green.shade700 : Colors.red.shade700;
    final signed = '${credit ? '+' : '-'}'
        '${formatMinor(movement.amountMinor, movement.currency)}';
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 4),
      child: ListTile(
        leading: Icon(
          credit ? Icons.arrow_downward : Icons.arrow_upward,
          color: color,
        ),
        title: Text(
          movement.description.isEmpty ? '(Sin descripcion)' : movement.description,
        ),
        subtitle: Text(
          '${formatValueDate(movement.valueDate)} - '
          '${credit ? 'Abono' : 'Cargo'}',
        ),
        trailing: Text(signed, style: TextStyle(color: color)),
      ),
    );
  }
}
