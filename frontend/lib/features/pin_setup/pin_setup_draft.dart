// Borrador en memoria del cierre del registro (F-T46).
//
// Fuente unica del PIN durante el flujo crear -> confirmar -> biometrica ->
// OTP: el PIN nace en crear, se valida en confirmar, viaja a la oferta
// biometrica junto a la eleccion del usuario y se envia al backend SOLO en
// `POST /auth/pin/setup` desde el paso OTP.
//
// Reglas (brief F-T46 + docs/16 reglas 7 y 10):
//  - Vive solo en memoria (`extra` de `GoRouterState`): nunca en la URL,
//    query ni logs.
//  - No se persiste en storage: el servidor es la autoridad del
//    consentimiento (`identity.credentials.biometric_enabled`, E1-T38).
//  - Al volver de confirmar a crear el borrador se descarta (crear parte
//    vacio) para no dejar PIN viejo ni campos bloqueados.
library;

/// PIN confirmado + eleccion biometrica del usuario, solo en memoria.
class PinSetupDraft {
  const PinSetupDraft({required this.pin, this.biometricEnabled = false});

  /// PIN de 6 digitos creado en el paso crear (confirmado en confirmar).
  final String pin;

  /// Consentimiento de la oferta biometrica (fig `0:704`): el valor del
  /// switch al continuar, `false` al omitir. Viaja como `biometric_enabled`
  /// en `POST /auth/pin/setup`.
  final bool biometricEnabled;

  /// `true` cuando hay un PIN con el que continuar el flujo.
  bool get isComplete => pin.isNotEmpty;

  PinSetupDraft copyWith({String? pin, bool? biometricEnabled}) =>
      PinSetupDraft(
        pin: pin ?? this.pin,
        biometricEnabled: biometricEnabled ?? this.biometricEnabled,
      );

  /// Lee el borrador desde `GoRouterState.extra` con compatibilidad hacia
  /// atras: el `String` suelto de F-T39 equivale a un borrador solo con PIN
  /// (consentimiento `false`, igual que el default de `E1-T38`).
  static PinSetupDraft fromExtra(Object? extra) {
    if (extra is PinSetupDraft) return extra;
    if (extra is String && extra.isNotEmpty) {
      return PinSetupDraft(pin: extra);
    }
    return const PinSetupDraft(pin: '');
  }
}
