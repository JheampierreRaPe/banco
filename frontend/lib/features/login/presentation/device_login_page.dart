// Pantalla "Iniciar sesion en este dispositivo" (F-T56, HU03/HU04).
//
// Flujo de 3 pasos sobre `/login/device` (solo cuando NO hay `userRef`):
//  1. email + DNI/RUC -> `request` (OTP `LOGIN` por email).
//  2. OTP de 6 digitos (se conserva hasta el paso final, sin backend).
//  3. PIN de 4-6 digitos -> `complete` atomico (OTP + PIN + binding) que
//     asocia el dispositivo y abre sesion (la guarda navega a `/home`).
//
// Diseno "Eucalipto y Ocre" (pendiente de mockup del dueno; se siguen las
// paginas login `0:860`/PIN `0:901` de `pantallas.fig`): solo tokens de F-T34
// (`AppColors`, `AppTypography`, `AppSpacing`) y componentes base (`AppCard`,
// `AppPrimaryButton`, `AppGhostButton`).
//
// Cliente delgado (docs/19): la pagina captura/transporta y refleja; el
// backend decide. Email/DNI/OTP/PIN nunca se loguean ni viajan en la ruta
// (docs/16 reglas 7 y 9); el PIN va en `TextInputType.number` con
// `obscureText` y sin sugerencias. Un UNICO mensaje generico cubre los
// fallos del paso 1 y 3 ("no pudimos verificar", sin filtrar la causa).
// "Olvide mi PIN" queda como enlace opcional a `/pin-reset` (siempre
// disponible, no es parte del flujo).
//
// El [DeviceLoginController] lo crea el llamador (ver `login_routes.dart`) y
// sigue siendo suyo: esta pagina NO lo destruye, solo se desuscribe.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_button.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../device_login_controller.dart';

/// Pantalla de 3 pasos del login en este dispositivo (F-T56).
class DeviceLoginPage extends StatefulWidget {
  const DeviceLoginPage({super.key, required this.controller});

  /// Controlador delgado (propiedad del llamador; ver `login_routes.dart`).
  /// `null` = flujo no disponible (estado vacio).
  final DeviceLoginController? controller;

  @override
  State<DeviceLoginPage> createState() => _DeviceLoginPageState();
}

class _DeviceLoginPageState extends State<DeviceLoginPage> {
  late final TextEditingController _email;
  late final TextEditingController _doc;
  late final TextEditingController _code;
  late final TextEditingController _pin;
  String _docType = 'DNI';

  int get _expectedDocLength => _docType == 'RUC' ? 11 : 8;

  @override
  void initState() {
    super.initState();
    _email = TextEditingController();
    _doc = TextEditingController();
    _code = TextEditingController();
    _pin = TextEditingController();
    widget.controller?.addListener(_onControllerChanged);
  }

  void _onControllerChanged() {
    if (!mounted) return;
    // Sin navegacion manual: la guarda (`refreshListenable` sesion +
    // identidad) redirige a `/home` al concederse la sesion.
    setState(() {});
  }

  @override
  void dispose() {
    widget.controller?.removeListener(_onControllerChanged);
    _email.dispose();
    _doc.dispose();
    _code.dispose();
    _pin.dispose();
    super.dispose();
  }

  Future<void> _submitIdentity() async {
    await widget.controller?.submitIdentity(
      email: _email.text,
      docType: _docType,
      documentNumber: _doc.text,
    );
  }

  Future<void> _submitCode() async {
    await widget.controller?.submitCode(_code.text);
  }

  Future<void> _submitPin() async {
    await widget.controller?.submitPin(_pin.text);
  }

  void _onDocTypeChanged(String? value) {
    if (value == null || value == _docType) return;
    setState(() {
      _docType = value;
      _doc.clear();
    });
    widget.controller?.retry();
  }

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    if (controller == null) {
      return Scaffold(
        backgroundColor: AppColors.surface,
        appBar:
            AppBar(title: const Text('Inicia sesión en este dispositivo')),
        body: SafeArea(
          child: EmptyView(
            message: 'El inicio de sesión en este dispositivo no está '
                'disponible en este momento.',
            actionLabel: 'Volver a iniciar sesión',
            onAction: () => context.go('/login'),
          ),
        ),
      );
    }
    final step = controller.step;
    final message = controller.errorMessage ?? controller.infoMessage;
    final isError = controller.errorMessage != null;
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Inicia sesión en este dispositivo')),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(AppSpacing.marginMobile),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                _titleFor(step),
                style: AppTypography.headlineMd.copyWith(
                  color: AppColors.onSurface,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                _subtitleFor(step),
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: AppSpacing.stackSm),
              Text(
                'Paso $step de 3',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
              const SizedBox(height: AppSpacing.stackMd),
              if (step == 1) _identityCard(controller),
              if (step == 2) _codeCard(controller),
              if (step == 3) _pinCard(controller),
              // Estado de error/contenido (docs/20 §7): mensaje UNICO
              // generico para los fallos del paso 1 o 3 (misma clave).
              if (message != null) ...[
                const SizedBox(height: AppSpacing.stackMd),
                Container(
                  padding: const EdgeInsets.all(AppSpacing.stackMd),
                  decoration: BoxDecoration(
                    color: isError
                        ? AppColors.errorContainer
                        : AppColors.primaryFixed,
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                  child: Text(
                    message,
                    key: const Key('device-login-message'),
                    style: AppTypography.bodyMd.copyWith(
                      color: isError
                          ? AppColors.onErrorContainer
                          : AppColors.onPrimaryFixed,
                    ),
                  ),
                ),
              ],
              const SizedBox(height: AppSpacing.stackSm),
              // Fallback (dura): "Olvide mi PIN" siempre disponible.
              AppGhostButton(
                key: const Key('device-login-pin-reset-link'),
                label: 'Olvidé mi PIN',
                onPressed: () => context.go('/pin-reset'),
              ),
            ],
          ),
        ),
      ),
    );
  }

  String _titleFor(int step) {
    switch (step) {
      case 2:
        return 'Revisa tu correo';
      case 3:
        return 'Ingresa tu PIN';
      default:
        return 'Inicia sesión en este dispositivo';
    }
  }

  String _subtitleFor(int step) {
    switch (step) {
      case 2:
        return 'Ingresa el código de 6 dígitos que te enviamos por correo.';
      case 3:
        return 'Confirma tu PIN para asociar este dispositivo y abrir '
            'tu sesión.';
      default:
        return 'Ingresa tu correo y tu documento para recibir un código.';
    }
  }

  Widget _identityCard(DeviceLoginController controller) {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Correo electrónico',
            style: AppTypography.labelMd.copyWith(
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          TextField(
            key: const Key('device-login-email'),
            controller: _email,
            keyboardType: TextInputType.emailAddress,
            style: AppTypography.bodyLg.copyWith(
              color: AppColors.onSurface,
            ),
            // Dato sensible: sin sugerencias ni autocorreccion; nunca se
            // loguea (docs/16 reglas 7 y 9).
            enableSuggestions: false,
            autocorrect: false,
            decoration: InputDecoration(
              hintText: 'ejemplo@correo.com',
              hintStyle: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: AppSpacing.stackMd,
                vertical: AppSpacing.stackSm + AppSpacing.unit,
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.outlineVariant,
                ),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.primary,
                  width: 2,
                ),
              ),
            ),
          ),
          const SizedBox(height: AppSpacing.stackMd),
          Text(
            'Tipo de documento',
            style: AppTypography.labelMd.copyWith(
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          DropdownButtonFormField<String>(
            key: const Key('device-login-doc-type'),
            initialValue: _docType,
            decoration: InputDecoration(
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: AppSpacing.stackMd,
                vertical: AppSpacing.stackSm + AppSpacing.unit,
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.outlineVariant,
                ),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.primary,
                  width: 2,
                ),
              ),
            ),
            items: const [
              DropdownMenuItem(value: 'DNI', child: Text('DNI')),
              DropdownMenuItem(value: 'RUC', child: Text('RUC')),
            ],
            onChanged: _onDocTypeChanged,
          ),
          const SizedBox(height: AppSpacing.stackMd),
          Text(
            _docType == 'RUC' ? 'Número de RUC' : 'Número de DNI',
            style: AppTypography.labelMd.copyWith(
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          TextField(
            key: const Key('device-login-document'),
            controller: _doc,
            keyboardType: TextInputType.number,
            maxLength: _expectedDocLength,
            style: AppTypography.bodyLg.copyWith(
              color: AppColors.onSurface,
            ),
            enableSuggestions: false,
            autocorrect: false,
            inputFormatters: <TextInputFormatter>[
              FilteringTextInputFormatter.digitsOnly,
              LengthLimitingTextInputFormatter(_expectedDocLength),
            ],
            decoration: InputDecoration(
              hintText: _docType == 'RUC' ? '20123456789' : '12345678',
              hintStyle: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: AppSpacing.stackMd,
                vertical: AppSpacing.stackSm + AppSpacing.unit,
              ),
              counterText: '',
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.outlineVariant,
                ),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.primary,
                  width: 2,
                ),
              ),
            ),
          ),
          const SizedBox(height: AppSpacing.stackMd),
          AppPrimaryButton(
            key: const Key('device-login-request-submit'),
            label: 'Enviar código',
            loading: controller.busy,
            onPressed: controller.busy ? null : _submitIdentity,
          ),
        ],
      ),
    );
  }

  Widget _codeCard(DeviceLoginController controller) {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Código de 6 dígitos',
            style: AppTypography.labelMd.copyWith(
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          TextField(
            key: const Key('device-login-code'),
            controller: _code,
            keyboardType: TextInputType.number,
            maxLength: 6,
            style: AppTypography.bodyLg.copyWith(
              color: AppColors.onSurface,
            ),
            // OTP sensible: sin sugerencias ni autocorreccion; nunca se
            // loguea.
            enableSuggestions: false,
            autocorrect: false,
            inputFormatters: <TextInputFormatter>[
              FilteringTextInputFormatter.digitsOnly,
              LengthLimitingTextInputFormatter(6),
            ],
            decoration: InputDecoration(
              hintText: '123456',
              hintStyle: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: AppSpacing.stackMd,
                vertical: AppSpacing.stackSm + AppSpacing.unit,
              ),
              counterText: '',
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.outlineVariant,
                ),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.primary,
                  width: 2,
                ),
              ),
            ),
          ),
          const SizedBox(height: AppSpacing.stackMd),
          AppPrimaryButton(
            key: const Key('device-login-code-submit'),
            label: 'Continuar',
            loading: controller.busy,
            onPressed: controller.busy ? null : _submitCode,
          ),
          AppGhostButton(
            label: 'Corregir correo y documento',
            onPressed: controller.busy ? null : controller.back,
          ),
        ],
      ),
    );
  }

  Widget _pinCard(DeviceLoginController controller) {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'PIN de 4 a 6 dígitos',
            style: AppTypography.labelMd.copyWith(
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          TextField(
            key: const Key('device-login-pin'),
            controller: _pin,
            keyboardType: TextInputType.number,
            obscureText: true,
            maxLength: 6,
            style: AppTypography.bodyLg.copyWith(
              color: AppColors.onSurface,
            ),
            // PIN sensible: sin sugerencias ni autocorreccion (docs/16
            // reglas 7 y 10); nunca se loguea.
            enableSuggestions: false,
            autocorrect: false,
            inputFormatters: <TextInputFormatter>[
              FilteringTextInputFormatter.digitsOnly,
              LengthLimitingTextInputFormatter(6),
            ],
            decoration: InputDecoration(
              hintText: '••••',
              hintStyle: AppTypography.bodyMd.copyWith(
                color: AppColors.secondaryText,
              ),
              filled: true,
              fillColor: AppColors.surfaceContainerLowest,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: AppSpacing.stackMd,
                vertical: AppSpacing.stackSm + AppSpacing.unit,
              ),
              counterText: '',
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.outlineVariant,
                ),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
                borderSide: const BorderSide(
                  color: AppColors.primary,
                  width: 2,
                ),
              ),
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'Nunca compartas tu PIN con nadie.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
          const SizedBox(height: AppSpacing.stackMd),
          AppPrimaryButton(
            key: const Key('device-login-pin-submit'),
            label: 'Ingresar en este dispositivo',
            loading: controller.busy,
            onPressed: controller.busy ? null : _submitPin,
          ),
          AppGhostButton(
            label: 'Corregir el código',
            onPressed: controller.busy ? null : controller.back,
          ),
        ],
      ),
    );
  }
}
