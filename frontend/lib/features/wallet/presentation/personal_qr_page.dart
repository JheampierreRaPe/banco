import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/app_status_chip.dart';
import '../../../core/widgets/app_text_field.dart';
import '../data/personal_qr_service.dart';
import '../models/personal_qr.dart';
import 'qr_payload_view.dart';

/// Pantalla del QR personal estatico (HU14, E4-T21).
///
/// - **Generar:** concepto opcional -> `POST /wallet/qr`
///   (`Idempotency-Key` opcional: se genera una vez por intento y el
///   Reintentar reutiliza la misma clave, sin duplicar el cargo).
///   Cualquier titular con cuenta `ACTIVE` (sin rol comercial).
/// - **Mostrar:** el QR se renderiza a partir del `qr_content` del
///   backend (sobre `BO-QR1`, `qr_flutter`): sin monto (lo digita el
///   pagador al escanear) y sin caducidad (reutilizable).
/// - **Estado:** boton `Actualizar estado` (`GET /wallet/qr/{id}`)
///   para reflejar el servidor.
/// - Cliente delgado estricto (docs/19 §4): muestra `qr_content`/
///   `status` tal cual del backend; no firma, no calcula montos/
///   estados ni decide el cobro.
///
/// Estados (docs/20 §7): formulario (contenido), generando (carga),
/// error accionable en espanol + Reintentar.
class PersonalQrPage extends StatefulWidget {
  const PersonalQrPage({super.key, this.service});

  final PersonalQrServiceBase? service;

  @override
  State<PersonalQrPage> createState() => _PersonalQrPageState();
}

class _PersonalQrPageState extends State<PersonalQrPage> {
  final _conceptController = TextEditingController();

  /// Clave del intento vigente: se genera una vez y se reutiliza en el
  /// Reintentar del mismo intento (sin duplicar). "Generar otro" inicia
  /// un intento nuevo con clave nueva.
  String _idempotencyKey = const Uuid().v4();

  bool _saving = false;
  String? _formError;
  PersonalQr? _created;

  /// Estado refrescado (`GET`, fuente de verdad del badge).
  PersonalQrStatus? _refreshed;
  bool _refreshing = false;

  @override
  void dispose() {
    _conceptController.dispose();
    super.dispose();
  }

  /// Badge: usa el `status` refrescado del servidor si existe, si no
  /// el del `POST` (ambos del backend; nunca calculado).
  String get _effectiveStatus =>
      _refreshed?.status ?? _created?.status ?? '';

  Future<void> _generate() async {
    if (_saving) return;
    if (_conceptController.text.length > 120) {
      setState(
        () => _formError = 'El concepto no puede superar 120 caracteres.',
      );
      return;
    }
    final service = widget.service;
    if (service == null) {
      setState(() => _formError = 'Servicio de QR personal no configurado.');
      return;
    }
    setState(() {
      _saving = true;
      _formError = null;
    });
    try {
      final created = await service.createPersonalQr(
        concept: _conceptController.text.trim().isEmpty
            ? null
            : _conceptController.text.trim(),
        idempotencyKey: _idempotencyKey,
      );
      if (!mounted) return;
      setState(() {
        _created = created;
        _refreshed = null;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _formError = e.message);
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _refresh() async {
    final service = widget.service;
    final created = _created;
    if (service == null || created == null || _refreshing) return;
    setState(() => _refreshing = true);
    try {
      final status = await service.getPersonalQrStatus(created.chargeId);
      if (!mounted) return;
      setState(() => _refreshed = status);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _formError = e.message);
    } finally {
      if (mounted) setState(() => _refreshing = false);
    }
  }

  void _generateAnother() {
    setState(() {
      _created = null;
      _refreshed = null;
      _formError = null;
      _idempotencyKey = const Uuid().v4();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Mi QR personal')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
        child: _created == null ? _buildForm() : _buildResult(),
      ),
    );
  }

  Widget _buildForm() {
    return AppCard(
      key: const Key('personal-qr-form'),
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Cobra sin ser comercio',
            style: AppTypography.titleMd.copyWith(fontSize: 17),
          ),
          const SizedBox(height: 4),
          Text(
            'Genera tu QR personal: quien lo escanee digita el monto y '
            'el dinero llega a tu cuenta. No vence y lo puedes reutilizar '
            'tantas veces como quieras.',
            style: AppTypography.bodyMd.copyWith(
              fontSize: 13,
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: 16),
          AppTextField(
            key: const Key('personal-qr-concept-field'),
            controller: _conceptController,
            label: 'Concepto (opcional)',
            hintText: 'Ej. Cumpleaños, almuerzo',
          ),
          const SizedBox(height: 16),
          if (_formError != null) ...[
            Text(
              _formError!,
              key: const Key('personal-qr-form-error'),
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.error,
                fontSize: 13,
              ),
            ),
            const SizedBox(height: 8),
          ],
          SizedBox(
            width: double.infinity,
            height: 52,
            child: FilledButton.icon(
              key: const Key('personal-qr-generate'),
              onPressed: _saving ? null : _generate,
              icon: _saving
                  ? const SizedBox.square(
                      dimension: 20,
                      child: CircularProgressIndicator(strokeWidth: 2.5),
                    )
                  : const Icon(Icons.qr_code_2, size: 20),
              label: Text(
                _saving ? 'Generando…' : 'Generar mi QR',
                style: const TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildResult() {
    final created = _created!;
    return Column(
      children: [
        AppCard(
          key: const Key('personal-qr-result'),
          padding: const EdgeInsets.all(20),
          child: Column(
            children: [
              Row(
                children: [
                  const Expanded(
                    child: Text(
                      'Tu QR personal',
                      style: TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  AppStatusChip.success(
                    label: _effectiveStatus == 'ACTIVE'
                        ? 'Activo'
                        : _effectiveStatus,
                  ),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                'Sin monto fijo y sin vencimiento: reutilizable.',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 13,
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: 12),
              QrPayloadView(qrContent: created.qrContent),
              const SizedBox(height: 12),
              SizedBox(
                width: double.infinity,
                child: OutlinedButton.icon(
                  key: const Key('personal-qr-refresh'),
                  onPressed: _refreshing ? null : _refresh,
                  icon: _refreshing
                      ? const SizedBox.square(
                          dimension: 18,
                          child: CircularProgressIndicator(strokeWidth: 2.5),
                        )
                      : const Icon(Icons.refresh, size: 18),
                  label: const Text('Actualizar estado'),
                ),
              ),
              if (_formError != null) ...[
                const SizedBox(height: 8),
                Text(
                  _formError!,
                  key: const Key('personal-qr-refresh-error'),
                  style: AppTypography.bodyMd.copyWith(
                    color: AppColors.error,
                    fontSize: 13,
                  ),
                ),
              ],
              TextButton.icon(
                key: const Key('personal-qr-another'),
                onPressed: _generateAnother,
                icon: const Icon(Icons.add, size: 18),
                label: const Text('Generar otro'),
              ),
            ],
          ),
        ),
      ],
    );
  }
}
