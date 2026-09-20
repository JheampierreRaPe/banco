import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/widgets/app_version_label.dart';

/// Pantalla de eleccion (puerta de entrada sin sesion).
///
/// UI minima funcional: dos botones grandes, "Iniciar sesión" (`/login`) y
/// "Crear cuenta" (flujo KYC existente, `/kyc`).
class EntryPage extends StatelessWidget {
  const EntryPage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Banca Online Integral')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    FilledButton(
                      onPressed: () => context.go('/login'),
                      child: const Text('Iniciar sesión'),
                    ),
                    const SizedBox(height: 16),
                    OutlinedButton(
                      onPressed: () => context.go('/kyc'),
                      child: const Text('Crear cuenta'),
                    ),
                  ],
                ),
              ),
              // Version visible del build (F-T30): discreta, al pie.
              const Center(child: AppVersionLabel()),
            ],
          ),
        ),
      ),
    );
  }
}
