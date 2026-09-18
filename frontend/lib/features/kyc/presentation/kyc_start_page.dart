import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../kyc_dependencies.dart';
import '../kyc_flow_controller.dart';

/// Paso 1 del KYC: tipo + numero de documento, luego pide el desafio.
///
/// Supuesto documentado: el backend pre-registro valida `DNI` (verificado en
/// `backend/tests/test_kyc_proxy.py`); `CE`/`Pasaporte` se ofrecen para la UX
/// y cualquier rechazo del servidor se muestra como motivo en el resultado.
class KycStartPage extends StatefulWidget {
  const KycStartPage({super.key, this.controller});

  /// Controlador inyectable (tests). Por defecto, el compartido de
  /// [KycDependencies] (cableado por el orquestador).
  final KycFlowController? controller;

  static const List<String> documentTypes = ['DNI', 'CE', 'Pasaporte'];

  @override
  State<KycStartPage> createState() => _KycStartPageState();
}

class _KycStartPageState extends State<KycStartPage> {
  final _formKey = GlobalKey<FormState>();
  final _numberController = TextEditingController();
  String _docType = KycStartPage.documentTypes.first;

  KycFlowController get _controller =>
      widget.controller ?? KycDependencies.controller;

  @override
  void dispose() {
    _numberController.dispose();
    super.dispose();
  }

  Future<void> _continue() async {
    final form = _formKey.currentState;
    if (form == null || !form.validate()) return;
    FocusScope.of(context).unfocus();
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
      await context.push('/kyc/task');
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
                    'Necesitamos tu documento y una prueba de vida guiada. '
                    'Tus capturas solo viven en memoria durante la '
                    'verificacion y nunca se guardan en el dispositivo.',
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
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}
