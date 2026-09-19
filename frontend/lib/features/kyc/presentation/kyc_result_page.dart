import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/session/session_identity_store.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../kyc_dependencies.dart';
import '../kyc_error_handler.dart';
import '../kyc_flow_controller.dart';
import '../kyc_models.dart';

/// Paso 3 del KYC: resultado y motivo de fallo si aplica.
///
/// En exito el alta continua con un solo OTP: guarda el `user_ref` (F-T20) y
/// navega a `/pin-setup?userRef=` (codigo del correo + PIN). El backend valida
/// el OTP y activa; el cliente solo navega (cliente delgado, docs/19).
class KycResultPage extends StatelessWidget {
  const KycResultPage({super.key, this.controller, this.identity});

  /// Controlador inyectable (tests). Por defecto, el compartido de
  /// [KycDependencies].
  final KycFlowController? controller;

  /// Store de identidad inyectable (tests). Por defecto, el fijado por el
  /// orquestador via [sessionIdentityStoreFactory].
  final SessionIdentityStore? identity;

  KycFlowController _resolve(BuildContext context) =>
      controller ?? KycDependencies.controller;

  @override
  Widget build(BuildContext context) {
    final c = _resolve(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Resultado de verificacion')),
      body: ListenableBuilder(
        listenable: c,
        builder: (context, _) {
          if (c.busy) {
            return const LoadingView(message: 'Enviando verificacion...');
          }
          final result = c.result;
          if (result == null) {
            if (c.errorMessage != null) {
              return ErrorView(
                message: c.errorMessage!,
                onRetry: () => _retry(context, c),
              );
            }
            return EmptyView(
              message: 'Aun no hay un resultado de verificacion.',
              actionLabel: 'Volver a las tareas',
              onAction: () => context.go('/kyc/task'),
            );
          }
          if (result.overallResult) {
            return Center(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(
                      Icons.check_circle_outline,
                      size: 72,
                      color: Colors.green,
                    ),
                    const SizedBox(height: 16),
                    const Text('Verificacion exitosa'),
                    const SizedBox(height: 24),
                    FilledButton(
                      key: const Key('kyc-result-continue'),
                      onPressed: () => _startPinSetup(context, result),
                      child: const Text('Crear mi PIN'),
                    ),
                  ],
                ),
              ),
            );
          }
          return Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(
                    Icons.cancel_outlined,
                    size: 72,
                    color: Colors.red,
                  ),
                  const SizedBox(height: 16),
                  const Text('No se pudo verificar tu identidad.'),
                  // F-T23: se indica QUE paso fallo y el motivo traducido del
                  // servidor (E1-T29).
                  if (result.failedStep != null) ...[
                    const SizedBox(height: 8),
                    Text(
                      'Paso fallido: ${result.failedStep}',
                      key: const Key('kycResultFailedStep'),
                      textAlign: TextAlign.center,
                    ),
                  ],
                  const SizedBox(height: 8),
                  Text(
                    'Motivo: ${kycReasonMessage(result.failureReason)}',
                    key: const Key('kycResultReason'),
                    textAlign: TextAlign.center,
                  ),
                  const SizedBox(height: 24),
                  FilledButton(
                    onPressed: () => _retry(context, c),
                    child: const Text('Reintentar verificacion'),
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }

  void _retry(BuildContext context, KycFlowController c) {
    c.reset();
    context.go('/kyc');
  }

  /// Guarda el `user_ref` del alta y navega a crear el PIN.
  ///
  /// F-T19 garantiza `user_id` en el exito; si faltara (backend legacy) no se
  /// inventa una referencia: se avisa y no se navega.
  Future<void> _startPinSetup(
    BuildContext context,
    KycSubmitResult result,
  ) async {
    final userId = result.userId;
    if (userId == null || userId.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            'No pudimos obtener tu identificador. Vuelve a intentarlo.',
          ),
        ),
      );
      return;
    }
    final store = identity ?? sessionIdentityStoreFactory?.call();
    if (store != null) {
      try {
        await store.saveUserRef(userId);
      } catch (_) {
        // Best-effort (simetria con `LoginController._rememberUser`): un fallo
        // del secure storage no bloquea el alta. No se inventa el user_ref; el
        // `userId` ya viaja en la ruta, asi que se avisa y se continua.
        if (context.mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(
              content: Text(
                'No pudimos guardar tu identificador en este dispositivo, '
                'pero puedes continuar.',
              ),
            ),
          );
        }
      }
    }
    if (!context.mounted) return;
    context.go('/pin-setup?userRef=${Uri.encodeComponent(userId)}');
  }
}
