import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/widgets/app_version_label.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../kyc_dependencies.dart';
import '../kyc_flow_controller.dart';
import '../kyc_models.dart';

/// Paso 1 del KYC: datos del titular + tipo/numero de documento, luego pide el
/// desafio.
///
/// Cliente delgado (F-T19, docs/19): solo captura y envia; el backend valida
/// identidad y decide. Los tipos de documento se ofrecen como `DNI|CE|
/// Pasaporte`; el mapeo `Pasaporte -> PASSPORT` lo hace la capa de servicio.
class KycStartPage extends StatefulWidget {
  const KycStartPage({super.key, this.controller});

  /// Controlador inyectable (tests). Por defecto, el compartido de
  /// [KycDependencies] (cableado por el orquestador).
  final KycFlowController? controller;

  static const List<String> documentTypes = kKycDocumentTypes;

  @override
  State<KycStartPage> createState() => _KycStartPageState();
}

class _KycStartPageState extends State<KycStartPage> {
  final _formKey = GlobalKey<FormState>();
  final _firstNameController = TextEditingController();
  final _lastNameController = TextEditingController();
  final _emailController = TextEditingController();
  final _phoneController = TextEditingController();
  final _numberController = TextEditingController();
  String _docType = KycStartPage.documentTypes.first;

  KycFlowController get _controller =>
      widget.controller ?? KycDependencies.controller;

  @override
  void dispose() {
    _firstNameController.dispose();
    _lastNameController.dispose();
    _emailController.dispose();
    _phoneController.dispose();
    _numberController.dispose();
    super.dispose();
  }

  /// Validacion de UI de email (formato). El backend es la autoridad final.
  static String? _validateEmail(String? value) {
    final email = (value ?? '').trim();
    if (email.isEmpty) return 'Ingresa tu email';
    final valid = RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(email);
    if (!valid) return 'Ingresa un email valido';
    return null;
  }

  Future<void> _continue() async {
    final form = _formKey.currentState;
    if (form == null || !form.validate()) return;
    FocusScope.of(context).unfocus();
    _controller.setApplicant(
      KycApplicant(
        firstName: _firstNameController.text.trim(),
        lastName: _lastNameController.text.trim(),
        email: _emailController.text.trim(),
        phone: _phoneController.text.trim(),
      ),
    );
    _controller.setDocument(
      type: _docType,
      number: _numberController.text.trim(),
    );
    await _controller.loadChallenge();
    if (!mounted) return;
    // Fallo de red: se queda en esta pantalla con el error visible y el
    // desafio previo intacto (no se navega).
    if (_controller.errorMessage != null) return;
    if (_controller.challenge != null) {
      // F-T23: primero el documento (camara trasera), luego el liveness.
      await context.push('/kyc/document');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Verifica tu identidad')),
      body: ListenableBuilder(
        listenable: _controller,
        builder: (context, _) {
          if (_controller.busy) {
            return const LoadingView(message: 'Solicitando desafio...');
          }
          return SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Form(
              key: _formKey,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Text(
                    'Necesitamos tus datos, tu documento y una prueba de vida '
                    'guiada. Tus capturas solo viven en memoria durante la '
                    'verificacion y nunca se guardan en el dispositivo.',
                  ),
                  const SizedBox(height: 24),
                  TextFormField(
                    key: const Key('firstNameField'),
                    controller: _firstNameController,
                    decoration: const InputDecoration(
                      labelText: 'Nombres',
                      border: OutlineInputBorder(),
                    ),
                    textInputAction: TextInputAction.next,
                    validator: (v) =>
                        (v ?? '').trim().isEmpty ? 'Ingresa tus nombres' : null,
                  ),
                  const SizedBox(height: 16),
                  TextFormField(
                    key: const Key('lastNameField'),
                    controller: _lastNameController,
                    decoration: const InputDecoration(
                      labelText: 'Apellidos',
                      border: OutlineInputBorder(),
                    ),
                    textInputAction: TextInputAction.next,
                    validator: (v) => (v ?? '').trim().isEmpty
                        ? 'Ingresa tus apellidos'
                        : null,
                  ),
                  const SizedBox(height: 16),
                  TextFormField(
                    key: const Key('emailField'),
                    controller: _emailController,
                    decoration: const InputDecoration(
                      labelText: 'Email',
                      border: OutlineInputBorder(),
                    ),
                    keyboardType: TextInputType.emailAddress,
                    textInputAction: TextInputAction.next,
                    validator: _validateEmail,
                  ),
                  const SizedBox(height: 16),
                  TextFormField(
                    key: const Key('phoneField'),
                    controller: _phoneController,
                    decoration: const InputDecoration(
                      labelText: 'Telefono (opcional)',
                      border: OutlineInputBorder(),
                    ),
                    keyboardType: TextInputType.phone,
                    textInputAction: TextInputAction.next,
                  ),
                  const SizedBox(height: 24),
                  DropdownButtonFormField<String>(
                    key: const Key('docTypeField'),
                    initialValue: _docType,
                    decoration: const InputDecoration(
                      labelText: 'Tipo de documento',
                      border: OutlineInputBorder(),
                    ),
                    items: [
                      for (final t in KycStartPage.documentTypes)
                        DropdownMenuItem(value: t, child: Text(t)),
                    ],
                    onChanged: (v) => setState(() => _docType = v ?? _docType),
                  ),
                  const SizedBox(height: 16),
                  TextFormField(
                    key: const Key('docNumberField'),
                    controller: _numberController,
                    decoration: const InputDecoration(
                      labelText: 'Numero de documento',
                      border: OutlineInputBorder(),
                    ),
                    keyboardType: TextInputType.text,
                    validator: (v) {
                      final value = (v ?? '').trim();
                      if (value.isEmpty) {
                        return 'Ingresa el numero de documento';
                      }
                      if (value.length < 6) {
                        return 'El numero parece incompleto';
                      }
                      return null;
                    },
                  ),
                  const SizedBox(height: 24),
                  FilledButton(
                    onPressed: _continue,
                    child: const Text('Continuar'),
                  ),
                  if (_controller.errorMessage != null) ...[
                    const SizedBox(height: 16),
                    ErrorView(
                      message: _controller.errorMessage!,
                      onRetry: _continue,
                    ),
                  ],
                  // Version visible del build (F-T30): discreta, al pie.
                  const SizedBox(height: 24),
                  const Center(child: AppVersionLabel()),
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}
