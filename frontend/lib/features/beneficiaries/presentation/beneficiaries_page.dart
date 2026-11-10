import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../data/beneficiaries_service.dart';
import '../models/beneficiary.dart';
import 'delete_beneficiary_dialog.dart';

/// Modo de agrupacion de la agenda (criterio de UI, no de negocio).
enum BeneficiaryGroupMode { byBank, alpha }

/// Agenda de beneficiarios (HU07, E2-T15; mockups `Agenda de Beneficiarios`,
/// `Agenda 2/3/4/5` en `pantallas.fig`).
///
/// - Busqueda en tiempo real con debounce contra `GET /beneficiaries?q=`
///   (cliente delgado: el servidor resuelve; el cliente no filtra bases
///   grandes ni calcula reglas, docs/19 §4).
/// - Agrupacion por banco o alfabetica (tabs del mockup).
/// - Datos siempre enmascarados (`accountMasked` tal cual del backend; el
///   CCI/cuenta completo jamas se muestra ni se loguea).
/// - Eliminar exige dialogo de confirmacion y solo tras 2xx se refleja
///   (HU07 CA-04).
///
/// Estados (mockups): lista, skeleton (carga), vacio, error + reintento.
class BeneficiariesPage extends StatefulWidget {
  const BeneficiariesPage({super.key, this.service});

  final BeneficiariesServiceBase? service;

  @override
  State<BeneficiariesPage> createState() => _BeneficiariesPageState();
}

class _BeneficiariesPageState extends State<BeneficiariesPage> {
  final _searchController = TextEditingController();
  Timer? _debounce;
  BeneficiaryGroupMode _mode = BeneficiaryGroupMode.byBank;

  Future<List<Beneficiary>>? _future;
  String _query = '';

  @override
  void initState() {
    super.initState();
    _future = _load('');
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _searchController.dispose();
    super.dispose();
  }

  Future<List<Beneficiary>> _load(String query) {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de beneficiarios no configurado.',
        ),
      );
    }
    return service.list(query: query.isEmpty ? null : query);
  }

  void _onSearchChanged(String value) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 350), () {
      final q = value.trim();
      setState(() {
        _query = q;
        _future = _load(q);
      });
    });
  }

  void _retry() {
    setState(() {
      _future = _load(_query);
    });
  }

  Future<void> _refresh() async {
    final future = _load(_query);
    setState(() {
      _future = future;
    });
    try {
      await future;
    } catch (_) {
      // El FutureBuilder muestra el error; el refresh solo reintenta.
    }
  }

  Future<void> _openForm({Beneficiary? beneficiary}) async {
    final path = beneficiary == null
        ? '/beneficiaries/new'
        : '/beneficiaries/${beneficiary.id}';
    final changed = await context.push<bool>(
      path,
      extra: beneficiary,
    );
    if (changed == true && mounted) _retry();
  }

  Future<void> _confirmDelete(Beneficiary beneficiary) async {
    final service = widget.service;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => DeleteBeneficiaryDialog(alias: beneficiary.alias),
    );
    if (confirmed != true || !mounted) return;
    if (service == null) return;
    try {
      // Solo tras 2xx se refleja el DELETED (optimista tras respuesta).
      await service.remove(beneficiary.id);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('${beneficiary.alias} eliminado.')),
      );
      _retry();
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(_friendlyError(e.code))),
      );
    }
  }

  void _showOptions(Beneficiary beneficiary) {
    showModalBottomSheet<void>(
      context: context,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.edit_outlined),
              title: const Text('Editar beneficiario'),
              onTap: () {
                Navigator.of(context).pop();
                _openForm(beneficiary: beneficiary);
              },
            ),
            ListTile(
              leading:
                  const Icon(Icons.delete_outline, color: AppColors.error),
              title: const Text(
                'Eliminar',
                style: TextStyle(color: AppColors.error),
              ),
              onTap: () {
                Navigator.of(context).pop();
                _confirmDelete(beneficiary);
              },
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(
        title: const Text('Agenda de Beneficiarios'),
        actions: [
          TextButton.icon(
            key: const Key('beneficiaries-new-action'),
            onPressed: () => _openForm(),
            icon: const Icon(Icons.add, size: 20),
            label: const Text('Nuevo'),
          ),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
            child: TextField(
              key: const Key('beneficiaries-search'),
              controller: _searchController,
              onChanged: _onSearchChanged,
              textInputAction: TextInputAction.search,
              decoration: InputDecoration(
                hintText: 'Buscar por alias, nombre o banco...',
                prefixIcon: const Icon(Icons.search),
                filled: true,
                fillColor: AppColors.surfaceContainerLowest,
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: BorderSide.none,
                ),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
            child: _GroupTabs(
              mode: _mode,
              onChanged: (mode) => setState(() => _mode = mode),
            ),
          ),
          Expanded(
            child: FutureBuilder<List<Beneficiary>>(
              future: _future,
              builder: (context, snapshot) {
                if (snapshot.connectionState == ConnectionState.waiting) {
                  return const _BeneficiariesSkeleton();
                }
                if (snapshot.hasError) {
                  final error = snapshot.error;
                  final message = error is ApiException
                      ? _errorMessage(error)
                      : 'Ocurrió un error al consultar el servidor. '
                          'Revisa tu conexión a internet o intenta de nuevo.';
                  return Padding(
                    padding: const EdgeInsets.all(16),
                    child: ErrorView(
                      message: message,
                      retryLabel: 'Reintentar conexión',
                      onRetry: _retry,
                    ),
                  );
                }
                final items =
                    (snapshot.data ?? const <Beneficiary>[])
                        .where((b) =>
                            b.status.toUpperCase() != 'DELETED')
                        .toList();
                if (items.isEmpty) {
                  return RefreshIndicator(
                    onRefresh: _refresh,
                    child: ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: [
                        const SizedBox(height: 48),
                        EmptyView(
                          message: 'Aún no tienes beneficiarios\n'
                              'Guarda tus cuentas frecuentes para transferir '
                              'en un toque sin volver a pedir números.',
                          actionLabel: 'Agregar beneficiario',
                          onAction: () => _openForm(),
                        ),
                      ],
                    ),
                  );
                }
                final groups = _mode == BeneficiaryGroupMode.byBank
                    ? groupByBank(items)
                    : groupByLetter(items);
                return RefreshIndicator(
                  onRefresh: _refresh,
                  child: ListView.builder(
                    physics: const AlwaysScrollableScrollPhysics(),
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
                    itemCount: groups.length,
                    itemBuilder: (context, index) {
                      final group = groups[index];
                      return _BeneficiaryGroupSection(
                        group: group,
                        mode: _mode,
                        onTap: (b) =>
                            _openForm(beneficiary: b),
                        onOptions: _showOptions,
                      );
                    },
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

/// Tabs `Por Banco` / `Alfabética (A-Z)` (mockup `SearchTabs`).
class _GroupTabs extends StatelessWidget {
  const _GroupTabs({required this.mode, required this.onChanged});

  final BeneficiaryGroupMode mode;
  final ValueChanged<BeneficiaryGroupMode> onChanged;

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
          _Tab(
            label: 'Por Banco',
            selected: mode == BeneficiaryGroupMode.byBank,
            onTap: () => onChanged(BeneficiaryGroupMode.byBank),
          ),
          _Tab(
            label: 'Alfabética (A-Z)',
            selected: mode == BeneficiaryGroupMode.alpha,
            onTap: () => onChanged(BeneficiaryGroupMode.alpha),
          ),
        ],
      ),
    );
  }
}

class _Tab extends StatelessWidget {
  const _Tab({
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
          height: 34,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: selected ? AppColors.primaryContainer : Colors.transparent,
            borderRadius: BorderRadius.circular(10),
          ),
          child: Text(
            label,
            style: AppTypography.labelMd.copyWith(
              fontSize: 13,
              fontWeight: selected ? FontWeight.w600 : FontWeight.w500,
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

/// Skeleton de carga (mockup `Agenda 2 - Skeleton`: barras grises `#E4E2DD`).
class _BeneficiariesSkeleton extends StatelessWidget {
  const _BeneficiariesSkeleton();

  @override
  Widget build(BuildContext context) {
    return ListView(
      key: const Key('beneficiaries-skeleton'),
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
      children: [
        Container(
          height: 12,
          width: 128,
          decoration: BoxDecoration(
            color: AppColors.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(6),
          ),
        ),
        const SizedBox(height: 12),
        for (var i = 0; i < 3; i++) ...[
          Container(
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
                      const SizedBox(height: 8),
                      Container(
                        height: 10,
                        width: 112,
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

class _BeneficiaryGroupSection extends StatelessWidget {
  const _BeneficiaryGroupSection({
    required this.group,
    required this.mode,
    required this.onTap,
    required this.onOptions,
  });

  final BeneficiaryGroup group;
  final BeneficiaryGroupMode mode;
  final ValueChanged<Beneficiary> onTap;
  final ValueChanged<Beneficiary> onOptions;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // E2-T33: la agenda ya no distingue por tope (sin etiquetas
          // por tope); solo el titulo del grupo.
          Text(
            group.title,
            style: AppTypography.labelMd.copyWith(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: AppColors.outline,
            ),
          ),
          const SizedBox(height: 8),
          for (final b in group.items) ...[
            _BeneficiaryCard(
              beneficiary: b,
              mode: mode,
              onTap: () => onTap(b),
              onOptions: () => onOptions(b),
            ),
            const SizedBox(height: 8),
          ],
        ],
      ),
    );
  }
}

/// Tarjeta de beneficiario (mockup: avatar + alias + titular + cuenta).
class _BeneficiaryCard extends StatelessWidget {
  const _BeneficiaryCard({
    required this.beneficiary,
    required this.mode,
    required this.onTap,
    required this.onOptions,
  });

  final Beneficiary beneficiary;
  final BeneficiaryGroupMode mode;
  final VoidCallback onTap;
  final VoidCallback onOptions;

  @override
  Widget build(BuildContext context) {
    final b = beneficiary;
    final subtitle2 = mode == BeneficiaryGroupMode.alpha
        ? '${bankShortName(b.bankCode)} · ${b.accountMasked}'
        : '${b.accountMasked} · ${currencyAccountLabel(b.currency)}';
    return AppCard(
      padding: const EdgeInsets.all(14),
      onTap: onTap,
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
                Row(
                  children: [
                    Flexible(
                      child: Text(
                        b.alias,
                        style: AppTypography.titleMd.copyWith(fontSize: 15),
                      ),
                    ),
                  ],
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
                  subtitle2,
                  style: AppTypography.bodyMd.copyWith(
                    fontSize: 12,
                    color: AppColors.outline,
                  ),
                ),
                const SizedBox(height: 6),
                // E2-T33: sin tope; toda transferencia sigue la unica regla
                // del umbral biometrico (S/ 1,000, lo decide el backend).
                const AppStatusChip.info(
                  label: 'Validación biométrica desde S/ 1,000',
                ),
              ],
            ),
          ),
          IconButton(
            key: Key('beneficiary-options-${b.id}'),
            icon: const Icon(Icons.more_vert, color: AppColors.outline),
            onPressed: onOptions,
          ),
        ],
      ),
    );
  }
}

/// Dialogo de eliminacion: ver [DeleteBeneficiaryDialog]
/// (mockup `Alta 5 - Eliminar`, compartido con el formulario).
///
/// Los fallos de red/servidor usan el texto del mockup `Agenda 4 - Error`;
/// los de negocio muestran su mensaje mapeado.
String _errorMessage(ApiException error) {
  if (error.isNetworkError ||
      error.code == 'UNKNOWN' ||
      error.code == 'SERVER_ERROR') {
    return 'Ocurrió un error al consultar el servidor. '
        'Revisa tu conexión a internet o intenta de nuevo.';
  }
  return error.message;
}

String _friendlyError(String code) {
  switch (code) {
    case 'DUPLICATE_BENEFICIARY':
      return 'Ese beneficiario ya esta registrado.';
    case 'VALIDATION_ERROR':
      return 'Revisa los datos ingresados e intentalo de nuevo.';
    case 'NOT_AUTHORIZED':
      return 'No tienes permiso para esta operacion.';
    case 'NOT_FOUND':
      return 'No encontramos ese beneficiario.';
    default:
      return 'Ocurrio un error inesperado. Intentalo mas tarde.';
  }
}
