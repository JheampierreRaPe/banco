import 'package:flutter/material.dart';

/// Placeholder de login (F-T01). F-T02/F-T03 implementan el login real.
///
/// Existe para que la guarda de sesion tenga a donde redirigir.
class LoginPlaceholderPage extends StatelessWidget {
  const LoginPlaceholderPage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Iniciar sesion')),
      body: const Center(
        child: Text('Pantalla de login (placeholder F-T01).'),
      ),
    );
  }
}
