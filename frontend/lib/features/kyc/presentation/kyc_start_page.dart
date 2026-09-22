import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/app_version_label.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../kyc_dependencies.dart';
import '../kyc_flow_controller.dart';
import '../kyc_models.dart';
import 'kyc_fig_widgets.dart';

/// Paso 1 del KYC: datos del titular + tipo/numero de documento, luego pide el
/// desafio.
///
/// Diseño canónico (F-T38, fig `0:789` "Registro: Datos Personales" y `0:407`
/// "Registro: Errores"): stepper `Paso 1 de 4 · Datos`, título
/// `Empecemos por ti`, tarjeta de formulario blanca, franja informativa y
/// pie con `Continuar`. Solo presentación: la lógica (validación de formato
/// en UI, `challenge`, navegación) no cambia.
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

  /// Muestra el banner de errores (fig `0:407`) tras un Continuar inválido.
  bool _showFormErrors = false;

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
    if (form == null || !form.validate()) {
      // Fig `0:407`: al fallar, banner + marcas de error en los campos.
      if (mounted) setState(() => _showFormErrors = true);
      return;
    }
    if (mounted) setState(() => _showFormErrors = false);
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
      backgroundColor: AppColors.surface,
      appBar: PreferredSize(
        preferredSize: const Size.fromHeight(AppSpacing.stackXl + AppSpacing.stackMd),
        child: KycTopBar(
          title: 'Crear cuenta',
          onBack: () => context.pop(),
        ),
      ),
      body: ListenableBuilder(
        listenable: _controller,
        builder: (context, _) {
          if (_controller.busy) {
            return const LoadingView(message: 'Solicitando desafio...');
          }
          return SingleChildScrollView(
            padding: const EdgeInsets.all(AppSpacing.stackMd),
            child: Form(
              key: _formKey,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const KycProgressStepper(
                    step: 1,
                    total: 4,
                    stageLabel: 'Datos',
                  ),
                  const SizedBox(height: AppSpacing.stackLg),
                  const KycSectionHeader(
                    title: 'Empecemos por ti',
                    subtitle:
                        'Ingresa tus datos tal como figuran en tu DNI.',
                  ),
                  const SizedBox(height: AppSpacing.stackMd),
                  if (_showFormErrors) ...[
                    const KycFormErrorBanner(
                      message: 'Revisa los campos marcados para continuar',
                    ),
                    const SizedBox(height: AppSpacing.stackMd),
                  ],
                  KycFormCard(
                    children: [
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const KycFieldLabel(text: 'Número de DNI'),
                          const SizedBox(height: AppSpacing.stackSm),
                          TextFormField(
                            key: const Key('docNumberField'),
                            controller: _numberController,
                            decoration: kycFieldDecoration(
                              hintText: '12345678',
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
                          const KycFieldHelper(text: '8 dígitos'),
                        ],
                      ),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const KycFieldLabel(text: 'Nombres'),
                          const SizedBox(height: AppSpacing.stackSm),
                          TextFormField(
                            key: const Key('firstNameField'),
                            controller: _firstNameController,
                            decoration: kycFieldDecoration(
                              hintText: 'Ej. Juan Carlos',
                            ),
                            textInputAction: TextInputAction.next,
                            validator: (v) => (v ?? '').trim().isEmpty
                                ? 'Ingresa tus nombres'
                                : null,
                          ),
                        ],
                      ),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const KycFieldLabel(text: 'Apellidos'),
                          const SizedBox(height: AppSpacing.stackSm),
                          TextFormField(
                            key: const Key('lastNameField'),
                            controller: _lastNameController,
                            decoration: kycFieldDecoration(
                              hintText: 'Ej. Pérez García',
                            ),
                            textInputAction: TextInputAction.next,
                            validator: (v) => (v ?? '').trim().isEmpty
                                ? 'Ingresa tus apellidos'
                                : null,
                          ),
                        ],
                      ),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const KycFieldLabel(text: 'Correo electrónico'),
                          const SizedBox(height: AppSpacing.stackSm),
                          TextFormField(
                            key: const Key('emailField'),
                            controller: _emailController,
                            decoration: kycFieldDecoration(
                              hintText: 'ejemplo@correo.com',
                            ),
                            keyboardType: TextInputType.emailAddress,
                            textInputAction: TextInputAction.next,
                            validator: _validateEmail,
                          ),
                          const KycFieldHelper(
                            text:
                                'Aquí te enviaremos tus constancias y el código '
                                'para recuperar tu PIN.',
                          ),
                        ],
                      ),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const KycFieldLabel(text: 'Tipo de documento'),
                          const SizedBox(height: AppSpacing.stackSm),
                          DropdownButtonFormField<String>(
                            key: const Key('docTypeField'),
                            initialValue: _docType,
                            decoration: kycFieldDecoration(hintText: ''),
                            items: [
                              for (final t in KycStartPage.documentTypes)
                                DropdownMenuItem(value: t, child: Text(t)),
                            ],
                            onChanged: (v) =>
                                setState(() => _docType = v ?? _docType),
                          ),
                        ],
                      ),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const KycFieldLabel(text: 'Teléfono (opcional)'),
                          const SizedBox(height: AppSpacing.stackSm),
                          TextFormField(
                            key: const Key('phoneField'),
                            controller: _phoneController,
                            decoration: kycFieldDecoration(
                              hintText: 'Ej. 999 888 777',
                            ),
                            keyboardType: TextInputType.phone,
                            textInputAction: TextInputAction.next,
                          ),
                        ],
                      ),
                    ],
                  ),
                  const SizedBox(height: AppSpacing.stackMd),
                  const KycInfoStrip(
                    message:
                        'Validaremos tu identidad con una foto de tu DNI y '
                        'reconocimiento facial.',
                  ),
                  const SizedBox(height: AppSpacing.stackLg),
                  AppPrimaryButton(
                    label: 'Continuar',
                    onPressed: _continue,
                  ),
                  if (_controller.errorMessage != null) ...[
                    const SizedBox(height: AppSpacing.stackMd),
                    ErrorView(
                      message: _controller.errorMessage!,
                      onRetry: _continue,
                    ),
                  ],
                  // Version visible del build (F-T30): discreta, al pie.
                  const SizedBox(height: AppSpacing.stackLg),
                  Text(
                    'Al continuar aceptas los Términos y la Política de Privacidad',
                    textAlign: TextAlign.center,
                    style: AppTypography.labelSm.copyWith(
                      color: AppColors.secondaryText,
                    ),
                  ),
                  const SizedBox(height: AppSpacing.stackSm),
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
