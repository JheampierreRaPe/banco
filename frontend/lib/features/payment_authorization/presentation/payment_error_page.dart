// Estados de error de pago (E2-T21, 6 mockups del dueno).
//
// Replica las pantallas `Error Pago 1..6` (`0:5571`..`0:5576`, fuente
// visual "Eucalipto y Ocre", Inter):
// - `TopBar`: cerrar (X) + `Estado del pago` + avatar.
// - `ErrorCard` (blanca, radio 12): icono en circulo de color, titulo
//   (20/600, `#152A1F`), causa breve (`#424844`) y franja de garantia
//   (`#F5F3EE`, radio 8).
// - `Resumen` (`TRANSFERENCIA PREVISTA` + filas + `Total intentado`).
// - `Tech` (`#F0EEE9`): detalle tecnico plegable con el codigo (el unico
//   lugar con tecnicismos) + referencia + `Copiar código de reporte`.
// - `Actions`: primaria (`#152A1F`) + secundaria + pie
//   `Transacción protegida CuyCash`.
//
// Auditoria i1 (decision documentada): NO existe pagina/ruta dedicada; solo
// el widget [PaymentErrorView], que `ConfirmPage` renderiza inline con sus
// callbacks propios (`_onPaymentPrimary`/`_onPaymentSecondary`). La ruta
// huerfana `/payments/error` y su envoltorio `PaymentErrorPage` se
// eliminaron (`payment_authorization_routes.dart`): rutear por `extra`
// perderia esos callbacks y romperia `ConfirmPage`, asi que el inline es el
// uso correcto y la ruta muerta no se reutiliza.
//
// Mapeo de codigos del backend (docs/05 §4, E2-T17/E2-T19):
// `INSUFFICIENT_FUNDS`, `DAILY_LIMIT_EXCEEDED`,
// `BIOMETRIC_PREVENTIVE_BLOCK`, `EXPIRED_NONCE`/sesion expirada,
// `TRANSFER_TIMEOUT`, `409` duplicado (`CONFLICT`/`IDEMPOTENCY_CONFLICT`).
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_spacing.dart';
import '../../../core/theme/app_typography.dart';
import '../../../core/widgets/app_card.dart';

/// Los 6 estados de error de pago (mockups `Error Pago 1..6`).
enum PaymentErrorKind {
  /// `INSUFFICIENT_FUNDS` — mockup 1.
  insufficientFunds,

  /// `DAILY_LIMIT_EXCEEDED` — mockup 2.
  dailyLimit,

  /// `BIOMETRIC_PREVENTIVE_BLOCK` — mockup 3.
  preventiveBlock,

  /// `EXPIRED_NONCE` / sesion expirada — mockup 4.
  sessionExpired,

  /// `TRANSFER_TIMEOUT` — mockup 5.
  timeout,

  /// `409` duplicado (`CONFLICT`/`IDEMPOTENCY_CONFLICT`) — mockup 6.
  duplicate;

  /// Resuelve el estado desde un codigo del backend (docs/05 §4).
  ///
  /// Desconocido/`null` -> [timeout] (reintento seguro: verificar antes
  /// de duplicar). `HOLDER_*` no llegan aqui (son del formulario CCI).
  static PaymentErrorKind fromCode(String? code) {
    switch ((code ?? '').toUpperCase()) {
      case 'INSUFFICIENT_FUNDS':
        return PaymentErrorKind.insufficientFunds;
      case 'DAILY_LIMIT_EXCEEDED':
        return PaymentErrorKind.dailyLimit;
      case 'BIOMETRIC_PREVENTIVE_BLOCK':
        return PaymentErrorKind.preventiveBlock;
      case 'EXPIRED_NONCE':
      case 'SESSION_INACTIVE':
      case 'NOT_AUTHENTICATED':
      case 'REFRESH_EXPIRED':
        return PaymentErrorKind.sessionExpired;
      case 'CONFLICT':
      case 'IDEMPOTENCY_CONFLICT':
      case 'DUPLICATE_REQUEST':
        return PaymentErrorKind.duplicate;
      case 'TRANSFER_TIMEOUT':
      case 'INTERBANK_UNAVAILABLE':
      case 'INTERBANK_REJECTED':
      default:
        return PaymentErrorKind.timeout;
    }
  }

  /// Posicion `N de 6` del `TopBar` (mockups `Estado del pago · N de 6`).
  int get position => index + 1;
}

/// Contenido editorial de cada estado (sin tecnicismos; el codigo solo va
/// en el detalle plegable).
class _Copy {
  const _Copy({
    required this.chip,
    required this.title,
    required this.cause,
    required this.reassurance,
    required this.icon,
    required this.iconColor,
    required this.iconBackground,
    required this.primaryLabel,
    required this.secondaryLabel,
    required this.primaryIcon,
    required this.techCode,
  });

  final String chip;
  final String title;
  final String cause;
  final String reassurance;
  final IconData icon;
  final Color iconColor;
  final Color iconBackground;
  final String primaryLabel;
  final String secondaryLabel;
  final IconData? primaryIcon;
  final String techCode;
}

_Copy _copyFor(PaymentErrorKind kind) {
  switch (kind) {
    case PaymentErrorKind.insufficientFunds:
      return const _Copy(
        chip: '',
        title: 'No tienes saldo suficiente',
        cause: 'El monto más la comisión supera tu saldo disponible.',
        reassurance: 'Tu dinero está seguro. No se debitó nada.',
        icon: Icons.account_balance_wallet_outlined,
        iconColor: AppColors.error,
        iconBackground: AppColors.errorContainer,
        // M12: acciones reales. La primaria vuelve al formulario (pop en
        // `ConfirmPage`): ahi se ajusta el monto con la cuenta actual. La
        // secundaria va a `/home` (ruta real): no se promete elegir otra
        // cuenta porque ese selector no existe en este flujo.
        primaryLabel: 'Volver y ajustar el monto',
        secondaryLabel: 'Volver al inicio',
        primaryIcon: null,
        techCode: 'ERR_INSUFFICIENT_FUNDS_400',
      );
    case PaymentErrorKind.dailyLimit:
      return const _Copy(
        chip: '',
        title: 'Superaste tu límite diario',
        cause:
            'Alcanzaste el máximo diario. '
            'Podrás transferir mañana desde las 00:00 hrs.',
        reassurance: 'Límite preventivo para proteger tus fondos.',
        icon: Icons.trending_up,
        iconColor: Color(0xFF895200),
        iconBackground: Color(0xFFFFDCBC),
        // M12: acciones reales. No hay pantalla de limites en `/profile`:
        // la primaria consulta el estado en Movimientos (`/movements`) y
        // la secundaria va a `/home`. Nada promete ajustar limites.
        primaryLabel: 'Ver movimientos',
        secondaryLabel: 'Volver al inicio',
        primaryIcon: null,
        techCode: 'ERR_DAILY_LIMIT_EXCEEDED_422',
      );
    case PaymentErrorKind.preventiveBlock:
      return const _Copy(
        chip: 'Protección biométrica preventiva',
        title: 'Acceso protegido por seguridad',
        cause:
            'Detectamos intentos no reconocidos. '
            'Protegimos tu cuenta temporalmente.',
        reassurance: 'Bloqueo preventivo. Fondos intactos.',
        icon: Icons.shield_outlined,
        iconColor: AppColors.error,
        iconBackground: AppColors.errorContainer,
        // M12: acciones reales. Con sesion activa `/login` redirige a
        // `/home` (no verifica nada) y no hay WhatsApp: la primaria va a
        // Movimientos (`/movements`) y la secundaria a `/home`.
        primaryLabel: 'Ver movimientos',
        secondaryLabel: 'Volver al inicio',
        primaryIcon: null,
        techCode: 'ERR_BIOMETRIC_PREVENTIVE_BLOCK_403',
      );
    case PaymentErrorKind.sessionExpired:
      return const _Copy(
        chip: '',
        title: 'La solicitud expiró',
        cause: 'La clave temporal caducó. Ningún fondo fue debitado.',
        reassurance: 'Inicia de nuevo con nueva clave temporal.',
        icon: Icons.timer_off_outlined,
        iconColor: AppColors.onSurfaceVariant,
        iconBackground: AppColors.surfaceContainerHigh,
        primaryLabel: 'Reintentar transferencia',
        secondaryLabel: 'Cancelar y salir',
        primaryIcon: Icons.repeat,
        techCode: 'ERR_EXPIRED_NONCE_TOKEN_401',
      );
    case PaymentErrorKind.timeout:
      return const _Copy(
        chip: 'En verificación (hasta 15 min)',
        title: 'La operación tardó demasiado',
        cause:
            'El banco receptor demoró. '
            'Verificamos si se completó para no cobrarte doble.',
        reassurance: 'No reintentes todavía. Verificación automática.',
        icon: Icons.hourglass_empty,
        iconColor: Color(0xFF895200),
        iconBackground: Color(0xFFFFDCBC),
        primaryLabel: 'Consultar estado en Movimientos',
        secondaryLabel: 'Regresar al inicio',
        primaryIcon: Icons.receipt_outlined,
        techCode: 'CCE_TIMEOUT_GATEWAY_504',
      );
    case PaymentErrorKind.duplicate:
      return const _Copy(
        chip: 'Operación no reversible',
        title: 'Ya fue procesada',
        cause:
            'Operación idéntica hace momentos. '
            'Evitamos débito duplicado.',
        reassurance: 'Anti-duplicidad activo. Solo primer intento.',
        icon: Icons.copy_outlined,
        iconColor: AppColors.primaryContainer,
        iconBackground: AppColors.surfaceContainerHighest,
        primaryLabel: 'Ver constancia del pago',
        secondaryLabel: 'Ir al inicio',
        primaryIcon: Icons.receipt_outlined,
        techCode: '409_IDEMPOTENCY_CONFLICT',
      );
  }
}

/// Argumentos de la pagina de error (viajan por `extra`; mascaras + UUIDs,
///
/// sin PII en claro: el CCI/titular completo nunca llega a la UI).
class PaymentErrorArgs {
  const PaymentErrorArgs({
    required this.kind,
    this.errorCode,
    this.recipientLabel = 'Destinatario',
    this.bankMasked = '',
    this.amountLabel = '',
    this.feeLabel = '',
    this.totalLabel = '',
    this.reportRef = '',
    this.reportDate = '',
  });

  final PaymentErrorKind kind;

  /// Codigo del backend para el detalle plegable (`null` = codigo del
  /// mockup).
  final String? errorCode;
  final String recipientLabel;
  final String bankMasked;
  final String amountLabel;
  final String feeLabel;
  final String totalLabel;

  /// Referencia del reporte (`cuy-tx-...`; `request_id` si se conoce).
  final String reportRef;
  final String reportDate;
}

/// Vista reutilizable de error de pago (las 6 variantes).
class PaymentErrorView extends StatelessWidget {
  const PaymentErrorView({
    super.key,
    required this.args,
    this.onPrimary,
    this.onSecondary,
    this.primaryLabel,
    this.secondaryLabel,
  });

  final PaymentErrorArgs args;
  final VoidCallback? onPrimary;
  final VoidCallback? onSecondary;

  /// Sobrescriben el texto de los botones del mockup (tests/flujos).
  final String? primaryLabel;
  final String? secondaryLabel;

  @override
  Widget build(BuildContext context) {
    final copy = _copyFor(args.kind);
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        AppCard(
          padding: const EdgeInsets.all(20),
          child: Column(
            children: [
              Container(
                width: 64,
                height: 64,
                alignment: Alignment.center,
                decoration: BoxDecoration(
                  color: copy.iconBackground,
                  shape: BoxShape.circle,
                ),
                child: Icon(copy.icon, size: 32, color: copy.iconColor),
              ),
              if (copy.chip.isNotEmpty) ...[
                const SizedBox(height: 12),
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 12,
                    vertical: 6,
                  ),
                  decoration: BoxDecoration(
                    color: copy.iconBackground,
                    borderRadius: BorderRadius.circular(AppRadii.md),
                  ),
                  child: Text(
                    copy.chip,
                    style: AppTypography.bodyMd.copyWith(
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                      color: copy.iconColor,
                    ),
                  ),
                ),
              ],
              const SizedBox(height: 12),
              Text(
                copy.title,
                key: const Key('payment-error-title'),
                textAlign: TextAlign.center,
                style: AppTypography.titleMd.copyWith(
                  fontSize: 20,
                  color: AppColors.primary,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                copy.cause,
                textAlign: TextAlign.center,
                style: AppTypography.bodyMd.copyWith(
                  color: AppColors.onSurfaceVariant,
                ),
              ),
              const SizedBox(height: 12),
              Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: AppColors.surfaceContainerLow,
                  borderRadius: BorderRadius.circular(AppRadii.def),
                ),
                child: Row(
                  children: [
                    const Icon(
                      Icons.verified_outlined,
                      size: 18,
                      color: AppColors.primary,
                    ),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        copy.reassurance,
                        style: AppTypography.bodyMd.copyWith(
                          fontSize: 12,
                          color: AppColors.onSurfaceVariant,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 12),
        AppCard(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                'TRANSFERENCIA PREVISTA',
                style: AppTypography.labelMd.copyWith(
                  fontSize: 12,
                  color: AppColors.primary,
                ),
              ),
              const SizedBox(height: 8),
              _kv('Destinatario', args.recipientLabel),
              if (args.bankMasked.isNotEmpty || args.amountLabel.isNotEmpty)
                _kv(
                  args.bankMasked.isEmpty ? 'Cuenta' : args.bankMasked,
                  args.amountLabel,
                  bold: true,
                ),
              if (args.feeLabel.isNotEmpty) _kv('Comisión CCE', args.feeLabel),
              Container(
                margin: const EdgeInsets.only(top: 8),
                padding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 8,
                ),
                decoration: BoxDecoration(
                  color: AppColors.surfaceContainer,
                  borderRadius: BorderRadius.circular(AppRadii.def),
                ),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(
                      'Total intentado',
                      style: AppTypography.bodyMd.copyWith(
                        fontSize: 12,
                        fontWeight: FontWeight.w600,
                        color: AppColors.primary,
                      ),
                    ),
                    Text(
                      args.totalLabel.isEmpty
                          ? args.amountLabel
                          : args.totalLabel,
                      style: AppTypography.bodyMd.copyWith(
                        fontWeight: FontWeight.bold,
                        color: AppColors.primary,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
        const SizedBox(height: 12),
        _TechDetail(
          code: args.errorCode ?? copy.techCode,
          reportRef: args.reportRef,
          reportDate: args.reportDate,
        ),
        const SizedBox(height: 16),
        SizedBox(
          width: double.infinity,
          child: copy.primaryIcon == null
              ? FilledButton(
                  key: const Key('payment-error-primary'),
                  onPressed: onPrimary,
                  style: FilledButton.styleFrom(
                    minimumSize: const Size(88, 52),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(AppRadii.md),
                    ),
                  ),
                  child: Text(primaryLabel ?? copy.primaryLabel),
                )
              : FilledButton.icon(
                  key: const Key('payment-error-primary'),
                  onPressed: onPrimary,
                  style: FilledButton.styleFrom(
                    minimumSize: const Size(88, 52),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(AppRadii.md),
                    ),
                  ),
                  icon: Icon(copy.primaryIcon, size: 18),
                  label: Text(primaryLabel ?? copy.primaryLabel),
                ),
        ),
        const SizedBox(height: 10),
        SizedBox(
          width: double.infinity,
          child: OutlinedButton(
            key: const Key('payment-error-secondary'),
            onPressed: onSecondary,
            style: OutlinedButton.styleFrom(
              minimumSize: const Size(88, 52),
              backgroundColor: AppColors.surfaceContainerLow,
              side: const BorderSide(color: AppColors.divider),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(AppRadii.md),
              ),
              foregroundColor: AppColors.primary,
            ),
            child: Text(
              secondaryLabel ?? copy.secondaryLabel,
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
          ),
        ),
        const SizedBox(height: 12),
        Center(
          child: Text(
            'Transacción protegida CuyCash',
            style: AppTypography.bodyMd.copyWith(
              fontSize: 11,
              color: AppColors.onSurfaceVariant,
            ),
          ),
        ),
      ],
    );
  }

  Widget _kv(String k, String v, {bool bold = false}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4, horizontal: 4),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(
            k,
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              color: AppColors.onSurfaceVariant,
            ),
          ),
          Flexible(
            child: Text(
              v,
              textAlign: TextAlign.end,
              style: AppTypography.bodyMd.copyWith(
                fontSize: 12,
                fontWeight: bold ? FontWeight.bold : FontWeight.w600,
                color: AppColors.onSurface,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Detalle tecnico plegable (unico lugar con el codigo; `ExpansionTile`
/// para no asustar: causa breve arriba, codigo solo si se despliega).
class _TechDetail extends StatelessWidget {
  const _TechDetail({
    required this.code,
    required this.reportRef,
    required this.reportDate,
  });

  final String code;
  final String reportRef;
  final String reportDate;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppColors.surfaceContainer,
        borderRadius: BorderRadius.circular(AppRadii.md),
      ),
      child: Material(
        type: MaterialType.transparency,
        child: ExpansionTile(
          key: const Key('payment-error-tech'),
          tilePadding: EdgeInsets.zero,
          childrenPadding: EdgeInsets.zero,
          title: Text(
            'Detalle técnico (Soporte)',
            style: AppTypography.bodyMd.copyWith(
              fontSize: 12,
              fontWeight: FontWeight.w600,
              color: AppColors.primary,
            ),
          ),
          children: [
            Align(
              alignment: Alignment.centerLeft,
              child: Text(
                code,
                key: const Key('payment-error-tech-code'),
                style: AppTypography.bodyMd.copyWith(
                  fontSize: 11,
                  fontWeight: FontWeight.w600,
                  color: AppColors.error,
                ),
              ),
            ),
            if (reportRef.isNotEmpty || reportDate.isNotEmpty) ...[
              const SizedBox(height: 4),
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  [
                    reportRef,
                    reportDate,
                  ].where((e) => e.isNotEmpty).join(' · '),
                  style: AppTypography.bodyMd.copyWith(
                    fontSize: 11,
                    color: AppColors.onSurfaceVariant,
                  ),
                ),
              ),
            ],
            const SizedBox(height: 8),
            SizedBox(
              width: double.infinity,
              child: FilledButton.tonal(
                key: const Key('payment-error-copy'),
                onPressed: () {
                  Clipboard.setData(ClipboardData(text: code));
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                      content: Text('Código copiado para el reporte.'),
                    ),
                  );
                },
                child: const Text('Copiar código de reporte'),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
