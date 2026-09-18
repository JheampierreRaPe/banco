import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import 'welcome_seen_store.dart';

/// Pantalla de bienvenida (solo la primera vez que se abre la app).
///
/// UI minima funcional: nombre de la banca, descripcion breve y boton
/// "Comenzar", que persiste el flag y navega a `/entry`.
class WelcomePage extends StatelessWidget {
  const WelcomePage({super.key, required this.seen});

  final WelcomeSeenStore seen;

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
            Text(
              'Banca Online Integral',
              style: Theme.of(context).textTheme.headlineSmall,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 12),
            const Text(
              'Tu banca digital para consultar saldos, mover tu dinero y '
              'verificar tu identidad de forma segura desde tu celular.',
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 24),
            FilledButton(
              onPressed: () async {
                await seen.markSeenWelcome();
                if (context.mounted) context.go('/entry');
              },
              child: const Text('Comenzar'),
            ),
          ],
        ),
      ),
    );
  }
}
