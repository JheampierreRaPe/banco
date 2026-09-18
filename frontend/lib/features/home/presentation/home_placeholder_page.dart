import 'package:flutter/material.dart';

/// Placeholder de inicio (F-T01). F-T05 implementa el dashboard real.
class HomePlaceholderPage extends StatelessWidget {
  const HomePlaceholderPage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Inicio')),
      body: const Center(
        child: Text('Pantalla de inicio (placeholder F-T01).'),
      ),
    );
  }
}
