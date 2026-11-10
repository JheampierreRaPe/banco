import 'package:flutter/material.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../data/notifications_service.dart';
import '../models/notification.dart';

/// Preferencias de canal del titular (HU22, E6-T15).
///
/// - Consulta (`GET /notifications/preferences`) y actualiza
///   (`PUT /notifications/preferences`) lo que responde el backend;
///   sin defaults locales (cliente delgado).
/// - El email de respaldo de seguridad es obligatorio: desactivar `email`
///   NO desactiva el respaldo de alertas de seguridad (nota visible).
///
/// Estados (docs/20 §7): carga, contenido, error + Reintentar. Mensajes
/// en espanol.
class NotificationPreferencesPage extends StatefulWidget {
  const NotificationPreferencesPage({super.key, this.service});

  final NotificationsServiceBase? service;

  @override
  State<NotificationPreferencesPage> createState() =>
      _NotificationPreferencesPageState();
}

class _NotificationPreferencesPageState
    extends State<NotificationPreferencesPage> {
  Future<ChannelPreferences>? _future;
  bool _push = true;
  bool _email = true;
  bool _sms = false;
  bool _loaded = false;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<ChannelPreferences> _load() {
    final service = widget.service;
    if (service == null) {
      return Future.error(
        ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de notificaciones no configurado.',
        ),
      );
    }
    return service.getPreferences();
  }

  void _retry() {
    setState(() {
      _loaded = false;
      _future = _load();
    });
  }

  void _applyLoaded(ChannelPreferences prefs) {
    if (_loaded) return;
    _loaded = true;
    _push = prefs.push;
    _email = prefs.email;
    _sms = prefs.sms;
  }

  /// Guarda y refleja la respuesta del backend (sin asumir locales).
  Future<void> _save() async {
    final service = widget.service;
    if (service == null || _saving) return;
    setState(() => _saving = true);
    try {
      final saved = await service.updatePreferences(
        ChannelPreferences(push: _push, email: _email, sms: _sms),
      );
      if (!mounted) return;
      setState(() {
        _push = saved.push;
        _email = saved.email;
        _sms = saved.sms;
      });
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Preferencias guardadas.'),
        ),
      );
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(e.message)),
      );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Preferencias de aviso')),
      body: FutureBuilder<ChannelPreferences>(
        future: _future,
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const LoadingView(message: 'Cargando preferencias…');
          }
          if (snapshot.hasError) {
            final error = snapshot.error;
            final message = error is ApiException
                ? error.message
                : 'Ocurrió un error al consultar el servidor. '
                    'Revisa tu conexión a internet o intenta de nuevo.';
            return Padding(
              padding: const EdgeInsets.all(16),
              child: ErrorView(
                message: message,
                retryLabel: 'Reintentar',
                onRetry: _retry,
              ),
            );
          }
          _applyLoaded(snapshot.data ??
              const ChannelPreferences(push: true, email: true, sms: false));
          return ListView(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
            children: [
              AppCard(
                padding: const EdgeInsets.all(8),
                child: Column(
                  children: [
                    SwitchListTile(
                      key: const Key('prefs-push'),
                      title: const Text('Notificaciones push'),
                      subtitle: const Text(
                        'Alertas en este dispositivo.',
                      ),
                      value: _push,
                      onChanged: _saving
                          ? null
                          : (v) => setState(() => _push = v),
                    ),
                    const Divider(height: 1),
                    SwitchListTile(
                      key: const Key('prefs-email'),
                      title: const Text('Correo'),
                      subtitle: const Text(
                        'Avisos por correo electrónico.',
                      ),
                      value: _email,
                      onChanged: _saving
                          ? null
                          : (v) => setState(() => _email = v),
                    ),
                    const Divider(height: 1),
                    SwitchListTile(
                      key: const Key('prefs-sms'),
                      title: const Text('SMS'),
                      subtitle: const Text('Avisos por mensaje de texto.'),
                      value: _sms,
                      onChanged:
                          _saving ? null : (v) => setState(() => _sms = v),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 12),
              Text(
                'El correo de respaldo de seguridad siempre se mantiene '
                'activo para las alertas de seguridad, aunque desactives '
                'el correo. Las preferencias rigen el resto de avisos.',
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 12,
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: 16),
              SizedBox(
                width: double.infinity,
                child: FilledButton(
                  key: const Key('prefs-save'),
                  style: FilledButton.styleFrom(
                    minimumSize: const Size(0, 52),
                  ),
                  onPressed: _saving ? null : _save,
                  child: Text(
                    _saving ? 'Guardando…' : 'Guardar preferencias',
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}
