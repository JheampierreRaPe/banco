/// Modelos del feature `wallet` (E4-T04 / HU13, contrato E4-T01 en
/// `docs/05` §6.5).
///
/// Shapes del backend:
/// - `GET /wallet` -> `{"data": {wallet_id, status, links[]}, "meta": {...}}`.
///   Sin billetera aun -> `200` con `wallet_id: null`, `status: "NONE"` y
///   `links: []` (la fila se crea on-demand en la primera vinculacion).
/// - `POST /wallet/links` -> `201 {"data": {vinculo}, "meta": {...}}`.
///   Vinculo = `{id, type (ACCOUNT|CARD), account_masked? (****1234, solo
///   cuentas), last4?/brand? (solo tarjetas), status, linked_at}`.
///
/// Reglas de oro (docs/16 §2.9, docs/13 §1.2):
/// - El PAN, el numero completo y el `token_ref` JAMAS viajan en respuestas:
///   estos modelos ni siquiera tienen campos para ellos (no se pueden
///   guardar ni mostrar por construccion).
/// - Solo se muestran datos enmascarados (`accountMasked`/`last4` tal cual
///   del backend); el cliente no calcula saldos, limites ni elegibilidad
///   (cliente delgado, docs/19 §4): muestra lo que responde el servidor.
library;

/// Vinculo de fuente de fondos enmascarado (nunca PAN ni `token_ref`).
class WalletLink {
  const WalletLink({
    required this.id,
    required this.type,
    this.accountMasked,
    this.last4,
    this.brand,
    required this.status,
    this.linkedAt,
  });

  final String id;

  /// `ACCOUNT` | `CARD`.
  final String type;

  /// `****1234` (solo cuentas; tal cual del backend).
  final String? accountMasked;

  /// Ultimos 4 (solo tarjetas; tal cual del backend).
  final String? last4;

  /// Marca (solo tarjetas; tal cual del backend).
  final String? brand;

  /// `ACTIVE` | `INACTIVE` | ... (lo decide el backend).
  final String status;
  final String? linkedAt;

  bool get isAccount => type.toUpperCase() == 'ACCOUNT';
  bool get isActive => status.toUpperCase() == 'ACTIVE';

  /// Etiqueta principal enmascarada (nunca el numero completo).
  String get maskedLabel {
    if (isAccount) return 'Cuenta ${accountMasked ?? '****'}';
    final tail = (last4 ?? '').isEmpty ? '••••' : '•••• ${last4!}';
    final prefix = (brand ?? '').isEmpty ? 'Tarjeta' : 'Tarjeta $brand';
    return '$prefix $tail';
  }

  factory WalletLink.fromJson(Map<String, dynamic> json) => WalletLink(
        id: json['id']?.toString() ?? '',
        type: (json['type']?.toString() ?? '').toUpperCase(),
        accountMasked: json['account_masked']?.toString(),
        last4: json['last4']?.toString(),
        brand: json['brand']?.toString(),
        status: (json['status']?.toString() ?? '').toUpperCase(),
        linkedAt: json['linked_at']?.toString(),
      );
}

/// Billetera del titular (tal cual del backend; sin recalculo).
class Wallet {
  const Wallet({
    this.walletId,
    required this.status,
    required this.links,
  });

  /// `null` = aun sin billetera (el backend responde `NONE` + `links: []`).
  final String? walletId;
  final String status;
  final List<WalletLink> links;

  bool get hasWallet => walletId != null && walletId!.isNotEmpty;

  factory Wallet.fromJson(Map<String, dynamic> json) {
    final raw = json['links'];
    final links = raw is List
        ? raw
            .whereType<Map>()
            .map((e) => WalletLink.fromJson(Map<String, dynamic>.from(e)))
            .toList()
        : <WalletLink>[];
    return Wallet(
      walletId: json['wallet_id']?.toString(),
      status: (json['status']?.toString() ?? 'NONE').toUpperCase(),
      links: links,
    );
  }

  /// Billetera vacia (sin vinculos), para el estado "vacio" de la UI.
  static const empty = Wallet(walletId: null, status: 'NONE', links: []);
}
