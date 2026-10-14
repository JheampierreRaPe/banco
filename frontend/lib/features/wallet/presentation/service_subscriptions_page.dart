import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/empty_view.dart';
import '../data/services_service.dart';
import '../models/services.dart';
import 'service_receipt_card.dart';

/// Servicios registrados y recordatorios (E4-T19, HU16 CA-03).
///
/// - Registra un servicio (`POST /services/subscriptions`: `biller` +
///   `supply_ref`; valida el suministro el servidor antes de persistir).
/// - Muestra los servicios registrados en esta sesion (estado efimero:
///   los que devolvio el backend al registrar, con su `status`; sin
///   persistencia local de dinero ni PII).
/// - El catalogo para elegir el `biller` sale de `GET
///   /services/catalog`.
///
/// Estados (docs/20 §7): contenido / carga / vacio / error con
/// Reintentar; mensajes en español; sin logica offline.
class ServiceSubscriptionsPage extends StatefulWidget {
  const ServiceSubscriptionsPage({super.key, this.service});

  final ServicesServiceBase? service;

  @override
  State<ServiceSubscriptionsPage> createState() =>
      _ServiceSubscriptionsPageState();
}

class _ServiceSubscriptionsPageState extends State<ServiceSubscriptionsPage> {
  bool _loading = true;
  List<Biller> _billers = [];
  final List<ServiceSubscription> _registered = [];
  ServiceFailure? _failure;

  String? _selectedBillerId;
  final TextEditingController _supply = TextEditingController();
  bool _saving = false;
  String? _formError;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _supply.dispose();
    super.dispose();
  }

  Future<void> _load() async {
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
      final billers = await service.catalog();
      if (!mounted) return;
      setState(() {
        _billers = billers;
        _selectedBillerId ??= billers.firstOrNull?.billerId;
        _loading = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _failure = serviceFailureFor(code: e.code, fallback: e.message);
      });
    }
  }

  String _billerName(String billerId) {
    for (final b in _billers) {
      if (b.billerId == billerId) return b.name;
    }
    return 'Servicio registrado';
  }

  Future<void> _register() async {
    final service = widget.service;
    final billerId = _selectedBillerId;
    final supply = _supply.text.trim();
    if (service == null || billerId == null || billerId.isEmpty) {
      setState(() => _formError = 'Elige la empresa del servicio.');
      return;
    }
    if (supply.isEmpty || supply.length > 80) {
      setState(() {
        _formError = 'Ingresa el número de suministro (1 a 80 caracteres).';
      });
      return;
    }
    setState(() {
      _saving = true;
      _formError = null;
    });
    try {
      final sub = await service.subscribe(
        billerId: billerId,
        supplyRef: supply,
      );
      if (!mounted) return;
      setState(() {
        _registered.insert(0, sub.withBillerName(_billerName(billerId)));
        _supply.clear();
        _saving = false;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        _formError = serviceFailureFor(code: e.code, fallback: e.message)
            .message;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Mis servicios')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        child: _buildBody(),
      ),
    );
  }

  Widget _buildBody() {
    if (_loading) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 48),
          child: CircularProgressIndicator(
            key: Key('subscriptions-loading'),
          ),
        ),
      );
    }
    final failure = _failure;
    if (failure != null && _billers.isEmpty) {
      return ServiceErrorCard(
        failure: failure,
        onRetry: failure.retry ? _load : null,
        onBack: () => context.pop(),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        AppCard(
          key: const Key('subscriptions-form'),
          padding: const EdgeInsets.all(20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('Registrar servicio', style: AppTypography.titleMd),
              const SizedBox(height: 4),
              Text(
                'Te avisaremos antes del vencimiento.',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 12,
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: 12),
              DropdownButtonFormField<String>(
                key: const Key('subscriptions-biller-field'),
                initialValue: _selectedBillerId,
                decoration: InputDecoration(
                  labelText: 'Empresa',
                  filled: true,
                  fillColor: AppColors.surfaceContainerLowest,
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                ),
                items: [
                  for (final b in _billers)
                    DropdownMenuItem(value: b.billerId, child: Text(b.name)),
                ],
                onChanged: (v) => setState(() => _selectedBillerId = v),
              ),
              const SizedBox(height: 8),
              TextField(
                key: const Key('subscriptions-supply-field'),
                controller: _supply,
                decoration: InputDecoration(
                  labelText: 'Número de suministro',
                  hintText: 'Ej. 12345678',
                  filled: true,
                  fillColor: AppColors.surfaceContainerLowest,
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                ),
              ),
              if (_formError != null) ...[
                const SizedBox(height: 8),
                Text(
                  _formError!,
                  key: const Key('subscriptions-form-error'),
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.onErrorContainer,
                  ),
                ),
              ],
              const SizedBox(height: 12),
              SizedBox(
                width: double.infinity,
                height: 52,
                child: FilledButton.icon(
                  key: const Key('subscriptions-register'),
                  onPressed: _saving ? null : _register,
                  icon: _saving
                      ? const SizedBox.square(
                          dimension: 20,
                          child: CircularProgressIndicator(strokeWidth: 2.5),
                        )
                      : const Icon(Icons.notifications_outlined, size: 20),
                  label: const Text('Registrar y activar recordatorio'),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 12),
        Text('Servicios registrados', style: AppTypography.titleMd),
        const SizedBox(height: 8),
        if (_registered.isEmpty)
          EmptyView(
            key: const Key('subscriptions-empty'),
            message: 'Aún no registras servicios.\n'
                'Registra uno arriba para recibir recordatorios.',
          )
        else
          for (final sub in _registered)
            AppCard(
              key: Key('subscriptions-item-${sub.subscriptionId}'),
              margin: const EdgeInsets.only(bottom: 8),
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
                      Icons.notifications_outlined,
                      color: AppColors.primary,
                      size: 24,
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          sub.billerName ?? _billerName(sub.billerId),
                          style: AppTypography.labelMd,
                        ),
                        const SizedBox(height: 2),
                        Text(
                          'Suministro ${sub.supplyRef} · avisa '
                          '${sub.notifyDaysBefore} día(s) antes',
                          style: AppTypography.bodyMd.copyWith(
                            fontSize: 12,
                            color: AppColors.secondaryText,
                          ),
                        ),
                      ],
                    ),
                  ),
                  AppStatusChip.success(label: sub.statusLabel),
                ],
              ),
            ),
      ],
    );
  }
}
