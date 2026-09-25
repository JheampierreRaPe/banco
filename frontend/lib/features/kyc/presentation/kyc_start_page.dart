import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import '../../../core/errors/api_exception.dart';
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
import '../kyc_service.dart';
import 'kyc_fig_widgets.dart';

/// Paso 1 del KYC: tipo/numero de documento validados por el servidor y datos
/// del titular, luego pide el desafio.
///
/// Diseño canónico (F-T44, pagina `crearCuenta-1Datos` `0:1201`, frame
/// `0:1256`): `FieldTipoDoc` primero (`0:1350`, `DNI · RUC`, default `DNI`) ->
/// `FieldNumDoc` (`0:1356`, solo digitos, 8/11 segun tipo) -> `BtnValidarDoc`
/// (`0:1361`, `Validar documento`) -> `FieldNombres` (`0:1363`) /
/// `FieldApellidos` (`0:1367`, **`readOnly`**, solo la API los llena) ->
/// `FieldCorreo` (`0:1371`) -> `FieldTelefono` (`0:1376`, opcional) ->
/// `InfoStrip` (`0:1302`).
///
/// Cliente delgado (docs/19 §4): la pantalla solo captura y envia; el titular
/// lo devuelve `POST /auth/kyc/document/lookup` del backend (E1-T35) y se
/// muestra tal cual (persona natural: `first_name`/`last_name`; RUC de persona
/// juridica: `business_name` donde iria el nombre). El frontend jamas llama a
/// la API externa ni conoce su key, y no registra PII en logs.
class KycStartPage extends StatefulWidget {
  const KycStartPage({super.key, this.controller, this.lookupService});

  /// Controlador inyectable (tests). Por defecto, el compartido de
  /// [KycDependencies] (cableado por el orquestador).
  final KycFlowController? controller;

  /// Servicio de consulta del titular (tests). Por defecto, el resuelto por
  /// [KycDependencies.lookupService] (el propio `HttpKycService`).
  final KycDocumentLookupService? lookupService;

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

  /// Muestra el banner de errores tras un Continuar inválido.
  bool _showFormErrors = false;

  /// `true` mientras `Validar documento` espera al servidor.
  bool _validating = false;

  /// `true` cuando el documento fue validado por el servidor y los nombres
  /// vigentes son los de esa respuesta. Cambiar tipo/numero lo invalida.
  bool _lookupValid = false;

  /// `true` cuando el titular validado es persona juridica (RUC con
  /// `business_name`, E1-T36/F-T48): el submit lleva `business_name` y la
  /// etiqueta del campo es "Razón social". Cambiar tipo/numero lo invalida.
  bool _lookupIsBusiness = false;

  /// Numero con el que se obtuvo [_lookupValid] (para invalidar al cambiarlo
  /// y no arrastrar datos obsoletos). El tipo se invalida en
  /// [_onDocTypeChanged].
  String? _validatedNumber;

  /// Error neutro de la ultima validacion (con reintento); `null` si no hay.
  String? _lookupError;

  /// Suscripción a la señal de reinicio del controller (F-T47).
  KycFlowController? _listenedController;
  int _lastResetGeneration = 0;

  KycFlowController get _controller =>
      widget.controller ?? KycDependencies.controller;

  /// Longitud exigida segun el tipo activo (DNI 8 / RUC 11).
  int get _expectedLength => _docType == 'RUC' ? 11 : 8;

  /// Ayuda dinamica del numero segun el tipo activo.
  String get _numberHelp => 'Solo números · $_expectedLength dígitos';

  /// `Validar documento` se habilita con el numero completo (solo digitos).
  bool get _canValidate =>
      !_validating &&
      _numberController.text.trim().length == _expectedLength;

  /// `Continuar` exige documento validado por el servidor con los datos
  /// vigentes, nombres llenos por la API y email valido (telefono opcional).
  bool get _canContinue {
    if (_validating || !_lookupValid) return false;
    if (_numberController.text.trim() != _validatedNumber) return false;
    if (_firstNameController.text.trim().isEmpty) return false;
    if (_validateEmail(_emailController.text) != null) return false;
    return true;
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // F-T47: la pantalla guarda estado local fuera del controller (email,
    // teléfono, nombres, número, tipo, flags de validación). Se suscribe a
    // la señal `resetGeneration`: ante un reinicio total (fallo final
    // silencioso o retroceso confirmado) limpia todo lo local para que
    // ningún dato de la corrida anterior reaparezca ni se salte la
    // revalidación del documento.
    KycFlowController? current;
    try {
      current = _controller;
    } on StateError {
      return;
    }
    if (!identical(current, _listenedController)) {
      _listenedController?.removeListener(_handleResetSignal);
      _listenedController = current;
      _lastResetGeneration = current.resetGeneration;
      current.addListener(_handleResetSignal);
    }
  }

  /// Limpia el estado local al detectar un reinicio del controller.
  void _handleResetSignal() {
    final current = _listenedController;
    if (current == null || !mounted) return;
    if (current.resetGeneration == _lastResetGeneration) return;
    _lastResetGeneration = current.resetGeneration;
    _firstNameController.clear();
    _lastNameController.clear();
    _emailController.clear();
    _phoneController.clear();
    _numberController.clear();
    setState(() {
      _docType = KycStartPage.documentTypes.first;
      _lookupValid = false;
      _lookupIsBusiness = false;
      _validatedNumber = null;
      _lookupError = null;
      _showFormErrors = false;
      _validating = false;
    });
  }

  @override
  void dispose() {
    _listenedController?.removeListener(_handleResetSignal);
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

  String? _validateNumber(String? value) {
    final number = (value ?? '').trim();
    if (number.isEmpty) return 'Ingresa el numero de documento';
    if (number.length != _expectedLength) {
      return 'El numero parece incompleto';
    }
    return null;
  }

  KycDocumentLookupService? _resolveLookup() {
    final injected = widget.lookupService;
    if (injected != null) return injected;
    try {
      return KycDependencies.lookupService;
    } on StateError {
      return null;
    }
  }

  /// Limpia los nombres recuperados y bloquea `Continuar` (datos obsoletos).
  void _invalidateLookup() {
    _lookupValid = false;
    _lookupIsBusiness = false;
    _validatedNumber = null;
    _lookupError = null;
    _firstNameController.clear();
    _lastNameController.clear();
  }

  void _onDocTypeChanged(String? value) {
    if (value == null || value == _docType) return;
    setState(() {
      _docType = value;
      _invalidateLookup();
    });
  }

  void _onNumberChanged(String value) {
    setState(() {
      if (_lookupValid && value.trim() != _validatedNumber) {
        _invalidateLookup();
      }
    });
  }

  /// `Validar documento`: consulta el titular al backend (E1-T35) y rellena
  /// `Nombres`/`Apellidos` (no editables). RUC de persona juridica: muestra
  /// `business_name` donde iria el nombre y deja apellidos vacio. En `404`,
  /// `409 DUPLICATE_DOCUMENT` (E1-T37, documento ya registrado: muestra el
  /// mensaje y bloquea `Continuar`, sin consulta ni avance) o fallo de red
  /// muestra un mensaje neutro con reintento y no habilita `Continuar`.
  /// Sin PII en logs: no se registra el numero ni la respuesta.
  Future<void> _validateDocument() async {
    final number = _numberController.text.trim();
    if (_validateNumber(number) != null) {
      if (mounted) setState(() => _showFormErrors = true);
      _formKey.currentState?.validate();
      return;
    }
    final lookup = _resolveLookup();
    if (lookup == null) {
      if (mounted) {
        setState(() {
          _lookupError =
              'No pudimos validar tu documento. Intentalo mas tarde.';
        });
      }
      return;
    }
    if (mounted) {
      setState(() {
        _validating = true;
        _lookupError = null;
      });
    }
    FocusScope.of(context).unfocus();
    try {
      final owner = await lookup.lookupDocument(
        type: _docType,
        number: number,
      );
      if (!mounted) return;
      setState(() {
        _validating = false;
        if (owner.isBusiness) {
          _firstNameController.text = owner.businessName;
          _lastNameController.clear();
        } else {
          _firstNameController.text = owner.firstName;
          _lastNameController.text = owner.lastName;
        }
        _lookupValid = true;
        _lookupIsBusiness = owner.isBusiness;
        _validatedNumber = number;
        _lookupError = null;
        _showFormErrors = false;
      });
    } on ApiException catch (e) {
      // Mensaje neutro del catalogo (sin eco del numero ni del proveedor).
      if (!mounted) return;
      setState(() {
        _validating = false;
        _invalidateLookup();
        _lookupError = e.message;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _validating = false;
        _invalidateLookup();
        _lookupError = 'Ocurrio un error inesperado. Intentalo mas tarde.';
      });
    }
  }

  Future<void> _continue() async {
    final form = _formKey.currentState;
    final number = _numberController.text.trim();
    if (!_lookupValid ||
        number != _validatedNumber ||
        form == null ||
        !form.validate()) {
      // Sin documento validado por el servidor no se avanza.
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
        // F-T48 (E1-T36): la razon social viaja como `business_name` en el
        // submit; en persona natural va vacio y el servidor la ignora.
        businessName: _lookupIsBusiness
            ? _firstNameController.text.trim()
            : '',
      ),
    );
    _controller.setDocument(
      type: _docType,
      number: number,
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
        preferredSize:
            const Size.fromHeight(AppSpacing.stackXl + AppSpacing.stackMd),
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
                        'Ingresa tus datos tal como figuran en tu documento.',
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
                            onChanged: _onDocTypeChanged,
                          ),
                          const KycFieldHelper(
                            text: 'Opciones: DNI · RUC',
                          ),
                        ],
                      ),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const KycFieldLabel(text: 'Número de documento'),
                          const SizedBox(height: AppSpacing.stackSm),
                          TextFormField(
                            key: const Key('docNumberField'),
                            controller: _numberController,
                            onChanged: _onNumberChanged,
                            decoration: kycFieldDecoration(
                              hintText: '12345678',
                            ),
                            keyboardType: TextInputType.number,
                            inputFormatters: [
                              FilteringTextInputFormatter.digitsOnly,
                            ],
                            maxLength: _expectedLength,
                            buildCounter: (
                              context, {
                              required int currentLength,
                              required bool isFocused,
                              int? maxLength,
                            }) =>
                                const SizedBox.shrink(),
                            validator: _validateNumber,
                          ),
                          KycFieldHelper(text: _numberHelp),
                        ],
                      ),
                      AppPrimaryButton(
                        key: const Key('validateDocumentButton'),
                        label: 'Validar documento',
                        loading: _validating,
                        onPressed:
                            _canValidate && !_validating ? _validateDocument : null,
                      ),
                      if (_lookupError != null)
                        KeyedSubtree(
                          key: const Key('kycLookupError'),
                          child: ErrorView(
                            message: _lookupError!,
                            onRetry:
                                _validating ? null : _validateDocument,
                          ),
                        ),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          // F-T48: con tipo RUC la etiqueta es "Razón social"
                          // (fig `crearCuenta-1Datos`); con DNI es "Nombres".
                          KycFieldLabel(
                            text: _docType == 'RUC'
                                ? 'Razón social'
                                : 'Nombres',
                          ),
                          const SizedBox(height: AppSpacing.stackSm),
                          TextFormField(
                            key: const Key('firstNameField'),
                            controller: _firstNameController,
                            readOnly: true,
                            decoration: kycFieldDecoration(
                              hintText: 'Ej. Juan Carlos',
                            ),
                            textInputAction: TextInputAction.next,
                            validator: (v) =>
                                (v ?? '').trim().isEmpty
                                    ? 'Valida tu documento para completar tus nombres'
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
                            readOnly: true,
                            decoration: kycFieldDecoration(
                              hintText: 'Ej. Pérez García',
                            ),
                            textInputAction: TextInputAction.next,
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
                            onChanged: (_) {
                              if (mounted) setState(() {});
                            },
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
                        'Validaremos tu identidad con una foto de tu documento y '
                        'reconocimiento facial.',
                  ),
                  const SizedBox(height: AppSpacing.stackLg),
                  AppPrimaryButton(
                    label: 'Continuar',
                    onPressed: _canContinue ? _continue : null,
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
