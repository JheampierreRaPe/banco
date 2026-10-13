import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';
import 'package:uuid/uuid.dart';

import '../../../core/errors/api_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';
import '../../../core/widgets/empty_view.dart';
import '../../../core/widgets/error_view.dart';
import '../../../core/widgets/loading_view.dart';
import '../../biometrics/biometric_reader.dart';
import '../data/credit_contract_service.dart';
import '../data/credit_sign_controller.dart';
import '../models/credit_contract.dart';

/// Pantalla de firma del contrato (E3-T15, HU11; spec
/// `docs/design/creditos-pantallas.md` §6, mockup `creditos-firma.png` +
/// Fig pagina `Créditos/Firma` `0:6760`, frame raiz `0:6761`).
///
/// - Fila expediente + badge `Por firmar` (`warning-container`).
/// - Card resumen (del servidor) + consentimiento explicito obligatorio
///   (sin el, los CTA estan bloqueados; el backend tambien lo valida).
/// - CTA `Firmar con biometría` (biometria del dispositivo via
///   [BiometricReader] + firma del `nonce` con la clave en almacenamiento
///   seguro, reutilizando `BiometricService.signNonce` de F-T03).
/// - Card Reto biométrico (`Esperando biometría…` / `Reintentar`).
/// - Card Fallback PIN (`PIN (6 dígitos)` + `Firmar con PIN`; contingencia,
///   el PIN nunca se guarda).
/// - Card éxito (`SIGNED` + copia enviada al correo + sello `doc_hash`) y
///   card error (`EXPIRED_NONCE` se rota y reintenta solo, con clave nueva).
/// - En éxito: botón `Volver al inicio` (→ `/home`, misma jerarquía que los
///   CTA existentes).
/// - 4 estados (`docs/20` §7) con `LoadingView`/`EmptyView`/`ErrorView` y
///   mensajes en espanol accionables.
///
/// Sin exito biometrico NO se envia la firma. `POST .../sign` usa
/// `ApiClient.postWithIdempotency` (misma clave ante reintentos del intento
/// en curso; clave nueva tras `EXPIRED_NONCE`).
class CreditSignPage extends StatefulWidget {
  const CreditSignPage({
    super.key,
    this.service,
    this.reader,
    this.signNonce,
    this.deviceId = '',
    this.applicationId,
    this.summary,
    this.initialChallenge,
  });

  final CreditContractServiceBase? service;
  final BiometricReader? reader;
  final Future<String> Function(String nonce)? signNonce;
  final String deviceId;
  final String? applicationId;
  final String? summary;
  final SignChallenge? initialChallenge;

  @override
  State<CreditSignPage> createState() => _CreditSignPageState();
}

class _CreditSignPageState extends State<CreditSignPage> {
  SignChallenge? _challenge;
  ApiException? _challengeError;
  bool _loadingChallenge = false;

  bool _consent = false;
  bool _signing = false;
  bool _signingPin = false;

  /// Ultimo desenlace biometrico (para la card Reto: mensaje + Reintentar).
  CreditSignState? _bioState;

  String? _pinError;
  final TextEditingController _pinController = TextEditingController();

  ContractSignatureResult? _signed;
  String _signedNote = '';

  /// Clave del intento biometrico en curso (se reutiliza ante reintentos;
  /// el reintento tras `EXPIRED_NONCE` deriva una NUEVA en el controlador).
  String? _bioKey;

  String get _applicationId =>
      widget.initialChallenge?.applicationId.isNotEmpty ?? false
      ? widget.initialChallenge!.applicationId
      : (widget.applicationId ?? '');

  String get _summary =>
      (widget.summary?.isNotEmpty ?? false) ? widget.summary! : '—';

  CreditSignController? get _controller {
    final service = widget.service;
    final reader = widget.reader;
    final signNonce = widget.signNonce;
    if (service == null || reader == null || signNonce == null) return null;
    return CreditSignController(
      service: service,
      reader: reader,
      signNonce: signNonce,
      deviceId: widget.deviceId,
    );
  }

  @override
  void initState() {
    super.initState();
    _challenge = widget.initialChallenge;
    if (_challenge != null) {
      if (_challenge!.isSigned) _markAlreadySigned();
    } else if (_applicationId.isNotEmpty) {
      _fetchChallenge();
    } else if (widget.service == null) {
      _challengeError = ApiException(
        code: 'UNKNOWN',
        message: 'Servicio de créditos no configurado.',
      );
    }
  }

  @override
  void dispose() {
    _pinController.dispose();
    super.dispose();
  }

  void _markAlreadySigned() {
    _signed = ContractSignatureResult(
      applicationId: _applicationId,
      contractId: '',
      status: 'SIGNED',
    );
    _signedNote = 'Este contrato ya fue firmado. Revisa tu correo.';
  }

  Future<void> _fetchChallenge() async {
    final service = widget.service;
    final id = _applicationId;
    if (id.isEmpty) return;
    setState(() {
      _loadingChallenge = true;
      _challengeError = null;
    });
    if (service == null) {
      setState(() {
        _loadingChallenge = false;
        _challengeError = ApiException(
          code: 'UNKNOWN',
          message: 'Servicio de créditos no configurado.',
        );
      });
      return;
    }
    try {
      final challenge = await service.requestChallenge(
        id,
        deviceId: widget.deviceId,
      );
      if (!mounted) return;
      setState(() {
        _challenge = challenge;
        _challengeError = null;
        _loadingChallenge = false;
      });
      if (challenge.isSigned) {
        setState(_markAlreadySigned);
      }
    } on ApiException catch (error) {
      if (!mounted) return;
      setState(() {
        _challengeError = error;
        _loadingChallenge = false;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _challengeError = ApiException(
          code: 'UNKNOWN',
          message: 'Ocurrió un error inesperado. Inténtalo más tarde.',
        );
        _loadingChallenge = false;
      });
    }
  }

  Future<void> _signWithBiometrics() async {
    if (_signing || _signed != null) return;
    final challenge = _challenge;
    if (challenge == null) return;
    final controller = _controller;
    if (controller == null) {
      setState(() {
        _bioState = const CreditSignState.pinFallback(
          creditBiometricUnavailableMessage,
        );
      });
      return;
    }
    _bioKey ??= const Uuid().v4();
    setState(() {
      _signing = true;
      _bioState = null;
    });
    final state = await controller.signWithBiometrics(
      applicationId: _applicationId,
      challenge: challenge,
      idempotencyKey: _bioKey,
      reason: 'Autoriza la firma del contrato de crédito',
    );
    if (!mounted) return;
    setState(() {
      _signing = false;
      switch (state.outcome) {
        case CreditSignOutcome.signed:
          _signed = state.result;
          _signedNote = state.message.isNotEmpty
              ? state.message
              : 'Contrato firmado. Copia certificada enviada a tu correo.';
        case CreditSignOutcome.requiresPinFallback:
        case CreditSignOutcome.expired:
        case CreditSignOutcome.failed:
          _bioState = state;
      }
    });
  }

  Future<void> _signWithPin() async {
    if (_signingPin || _signed != null) return;
    final pin = _pinController.text.trim();
    if (!RegExp(r'^\d{6}$').hasMatch(pin)) {
      setState(() => _pinError = creditPinInvalidMessage);
      return;
    }
    final controller = _controller;
    if (controller == null) {
      setState(
        () => _pinError = 'Servicio de créditos no configurado.',
      );
      return;
    }
    setState(() {
      _signingPin = true;
      _pinError = null;
    });
    // Cada intento PIN usa clave propia: el cuerpo cambia entre intentos
    // (el usuario puede corregir el PIN) y misma clave + cuerpo distinto
    // devuelve `409`. El PIN nunca se guarda.
    final state = await controller.signWithPin(
      applicationId: _applicationId,
      pin: pin,
      idempotencyKey: const Uuid().v4(),
    );
    if (!mounted) return;
    setState(() {
      _signingPin = false;
      switch (state.outcome) {
        case CreditSignOutcome.signed:
          _signed = state.result;
          _signedNote = 'Contrato firmado. Copia certificada enviada a tu correo.';
        case CreditSignOutcome.requiresPinFallback:
        case CreditSignOutcome.expired:
        case CreditSignOutcome.failed:
          _pinError = state.message;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.surface,
      appBar: AppBar(title: const Text('Firma del contrato')),
      body: _buildBody(),
    );
  }

  Widget _buildBody() {
    if (_applicationId.isEmpty) {
      return EmptyView(
        key: const Key('sign-empty'),
        message: 'Sin expediente para firmar. '
            'Vuelve e inicia el flujo de nuevo.',
        actionLabel: 'Volver',
        onAction: () => context.pop(),
      );
    }
    if (_loadingChallenge && _challenge == null) {
      return const LoadingView(
        key: Key('sign-loading'),
        message: 'Generando tu reto de firma…',
      );
    }
    final challengeError = _challengeError;
    if (challengeError != null && _challenge == null) {
      return ErrorView(
        key: const Key('sign-error'),
        message: creditContractUserMessage(challengeError),
        onRetry: _fetchChallenge,
      );
    }
    return ListView(
      key: const Key('sign-content'),
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
      children: [
        _expedientRow(),
        const SizedBox(height: AppSpacing.stackMd),
        _consentCard(),
        const SizedBox(height: AppSpacing.stackMd),
        SizedBox(
          width: double.infinity,
          child: FilledButton(
            key: const Key('sign-biometric-cta'),
            onPressed: !_consent || _signing || _signed != null
                ? null
                : _signWithBiometrics,
            style: FilledButton.styleFrom(
              minimumSize: const Size(88, 52),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
              ),
            ),
            child: Text(
              _signing ? 'Firmando…' : 'Firmar con biometría',
            ),
          ),
        ),
        const SizedBox(height: AppSpacing.stackMd),
        _challengeCard(),
        const SizedBox(height: AppSpacing.stackMd),
        _pinCard(),
        const SizedBox(height: AppSpacing.stackMd),
        if (_signed != null) ...[
          _successCard(),
          const SizedBox(height: AppSpacing.stackSm),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('sign-home-cta'),
              onPressed: () => context.go('/home'),
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
              ),
              child: const Text('Volver al inicio'),
            ),
          ),
          const SizedBox(height: AppSpacing.stackMd),
        ],
        if (_isErrorVisible) ...[
          _errorCard(),
          const SizedBox(height: AppSpacing.stackMd),
        ],
        Text(
          'Cargando, vacío y error (§7) con Reintentar.',
          style: AppTypography.bodyMd.copyWith(
            color: AppColors.secondaryText,
          ),
        ),
      ],
    );
  }

  // --fila_expediente_+_badge-----------------------------------------------------

  Widget _expedientRow() {
    return Row(
      children: [
        Expanded(
          child: Text(
            'EXPEDIENTE $_applicationId',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.onSurface,
            ),
          ),
        ),
        Container(
          key: const Key('sign-badge'),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          decoration: BoxDecoration(
            color: _signed != null
                ? AppColors.successContainer
                : AppColors.warningContainer,
            borderRadius: BorderRadius.circular(999),
          ),
          child: Text(
            _signed != null ? 'Firmado' : 'Por firmar',
            style: AppTypography.labelMd.copyWith(
              color: _signed != null
                  ? AppColors.onSuccessContainer
                  : AppColors.onWarningContainer,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ],
    );
  }

  // --resumen_+_consentimiento-------------------------------------------------------

  Widget _consentCard() {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainerLow,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'RESUMEN (del servidor)',
            style: AppTypography.labelSm.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.onSurfaceVariant,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            key: const Key('sign-summary'),
            _summary,
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurface,
            ),
          ),
          // `Material` propio (no pinta sobre el `DecoratedBox` del
          // contenedor): el `ListTile` pinta su fondo/ink en el `Material`
          // ancestro mas cercano (mismo patron que la solicitud, E3-T09).
          Material(
            color: Colors.transparent,
            child: CheckboxListTile(
              key: const Key('sign-consent'),
              value: _consent,
              onChanged: _signed != null
                  ? null
                  : (value) =>
                      setState(() => _consent = value ?? false),
              controlAffinity: ListTileControlAffinity.leading,
              contentPadding: EdgeInsets.zero,
              activeColor: AppColors.primary,
              title: Text(
                'He leído los 3 documentos y acepto firmarlos *',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.primary,
                  fontWeight: FontWeight.w600,
                ),
              ),
              subtitle: Text(
                'Obligatorio. Sin esto no se puede firmar.',
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.secondaryText,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  // --reto_biometrico-----------------------------------------------------------------

  String get _challengeStatusText {
    if (_signed != null) return 'Firma completada.';
    if (_signing) return 'Esperando biometría…';
    final bio = _bioState;
    if (bio != null && bio.message.isNotEmpty) return bio.message;
    return 'Toca el sensor para autorizar la firma.';
  }

  bool get _canRetryBio =>
      _signed == null &&
      !_signing &&
      _bioState != null &&
      _bioState!.outcome != CreditSignOutcome.signed;

  Widget _challengeCard() {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Reto biométrico',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          if (_signing) const LinearProgressIndicator(),
          Text(
            key: const Key('sign-challenge-status'),
            _challengeStatusText,
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSurface,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            'Si se cancela verás Reintentar. No revela el motivo.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.errorCarmine,
            ),
          ),
          if (_canRetryBio) ...[
            const SizedBox(height: AppSpacing.stackSm),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton(
                key: const Key('sign-retry'),
                onPressed: _signWithBiometrics,
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(88, 48),
                ),
                child: const Text('Reintentar'),
              ),
            ),
          ],
        ],
      ),
    );
  }

  // --fallback_PIN----------------------------------------------------------------------

  Widget _pinCard() {
    return AppCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '¿Sin biometría? Usa tu PIN',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            'PIN (6 dígitos)',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w600,
              color: AppColors.primary,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          TextField(
            key: const Key('sign-pin-input'),
            controller: _pinController,
            obscureText: true,
            keyboardType: TextInputType.number,
            maxLength: 6,
            enabled: _signed == null,
            inputFormatters: [FilteringTextInputFormatter.digitsOnly],
            decoration: const InputDecoration(
              hintText: '••••••',
              counterText: '',
            ),
            onChanged: (_) {
              if (_pinError != null) setState(() => _pinError = null);
            },
          ),
          if (_pinError != null) ...[
            const SizedBox(height: 4),
            Text(
              key: const Key('sign-pin-error'),
              _pinError!,
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.errorCarmine,
              ),
            ),
          ],
          const SizedBox(height: AppSpacing.stackSm),
          SizedBox(
            width: double.infinity,
            child: OutlinedButton(
              key: const Key('sign-pin-cta'),
              onPressed: !_consent || _signingPin || _signed != null
                  ? null
                  : _signWithPin,
              style: OutlinedButton.styleFrom(
                minimumSize: const Size(88, 52),
              ),
              child: Text(
                _signingPin ? 'Firmando…' : 'Firmar con PIN',
              ),
            ),
          ),
          const SizedBox(height: 4),
          Text(
            'Contingencia. El PIN nunca se guarda.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.secondaryText,
            ),
          ),
        ],
      ),
    );
  }

  // --exito_y_error-----------------------------------------------------------------------

  Widget _successCard() {
    final signed = _signed!;
    return Container(
      key: const Key('sign-success'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.successContainer,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '¡Documento firmado!',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.onSuccessContainer,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            _signedNote.isNotEmpty ? _signedNote : 'Contrato SIGNED. '
                'Copia enviada al correo.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSuccessContainer,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            'Sello doc_hash verificado (no repudio).'
            '${signed.signedAt != null ? ' ${signed.signedAt}' : ''}',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onSuccessContainer,
            ),
          ),
        ],
      ),
    );
  }

  bool get _isErrorVisible {
    final bio = _bioState;
    if (_signed != null || bio == null) return false;
    return bio.outcome == CreditSignOutcome.failed ||
        bio.outcome == CreditSignOutcome.expired;
  }

  Widget _errorCard() {
    final bio = _bioState!;
    return Container(
      key: const Key('sign-error-card'),
      width: double.infinity,
      padding: const EdgeInsets.all(AppSpacing.containerPadding),
      decoration: BoxDecoration(
        color: AppColors.errorContainer,
        borderRadius: BorderRadius.circular(AppRadii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'Si algo falla verás esto',
            style: AppTypography.labelMd.copyWith(
              fontWeight: FontWeight.w700,
              color: AppColors.onErrorContainer,
            ),
          ),
          const SizedBox(height: AppSpacing.stackSm),
          Text(
            bio.message.isNotEmpty ? bio.message : 'No se pudo completar la firma.',
            style: AppTypography.bodyMd.copyWith(
              color: AppColors.onErrorContainer,
            ),
          ),
          if (bio.code == 'EXPIRED_NONCE') ...[
            const SizedBox(height: 4),
            Text(
              'Reto vencido: se rota y se reintenta solo.',
              style: AppTypography.bodyMd.copyWith(
                color: AppColors.onErrorContainer,
              ),
            ),
          ],
        ],
      ),
    );
  }
}
