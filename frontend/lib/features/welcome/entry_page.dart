import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

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
      body: Padding(
        padding: const EdgeInsets.all(24),
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
    );
  }
}
