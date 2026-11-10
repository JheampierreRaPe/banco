/// Modelos del juego contractual y la firma (E3-T15, HU11).
///
/// Cliente delgado (`docs/19` §4): estos modelos solo transportan lo que
/// responde el backend (`GET .../contract`, `POST .../sign/challenge` y
/// `POST .../sign`, `docs/05` §6.4); el dispositivo NUNCA interpreta el
/// contrato ni recalcula cifras. Sin PII en disco: solo metadatos efimeros
/// (`doc_hash`, `doc_ref`) y bytes del PDF en memoria.
library;

/// Estado del juego contractual (espejo del servidor, `docs/05` §6.4).
enum ContractStatus {
  generated,
  signed;

  /// Parsea el `status` del servidor (`GENERATED` / `SIGNED`).
  ///
  /// Un valor desconocido colapsa a [generated] (jamas se promete una firma
  /// que el servidor no sello).
  static ContractStatus fromServer(String? raw) {
    switch (raw?.toUpperCase()) {
      case 'SIGNED':
        return ContractStatus.signed;
      case 'GENERATED':
      default:
        return ContractStatus.generated;
    }
  }
}

/// Un documento del juego contractual (contrato, pagare o resumen).
class ContractDocument {
  const ContractDocument({
    required this.contractId,
    required this.docType,
    required this.doc,
    required this.docRef,
    required this.docHash,
    required this.status,
    this.signedAt,
    this.seal,
  });

  final String contractId;

  /// Tipo del servidor (`CONTRATO` / `PAGARE` / `RESUMEN`).
  final String docType;

  /// Clave de descarga (`contrato` / `pagare` / `resumen`).
  final String doc;
  final String docRef;
  final String docHash;
  final ContractStatus status;
  final String? signedAt;
  final String? seal;

  /// Nombre visible segun la spec (`docs/design/creditos-pantallas.md` §5).
  String get displayName {
    switch (doc.toLowerCase()) {
      case 'pagare':
        return 'Pagaré';
      case 'resumen':
        return 'Hoja resumen';
      case 'contrato':
      default:
        return 'Contrato de crédito';
    }
  }

  /// Meta visible segun la spec (§5, solo display, sin recalcular nada).
  String get displayMeta {
    switch (doc.toLowerCase()) {
      case 'pagare':
        return 'Se firma junto al contrato - PDF';
      case 'resumen':
        return 'Condiciones y cronograma - PDF';
      case 'contrato':
      default:
        return '12 cláusulas - PDF del servidor';
    }
  }

  factory ContractDocument.fromJson(Map<String, dynamic> json) {
    return ContractDocument(
      contractId: '${json['contract_id'] ?? ''}',
      docType: '${json['doc_type'] ?? ''}',
      doc: '${json['doc'] ?? ''}',
      docRef: '${json['doc_ref'] ?? ''}',
      docHash: '${json['doc_hash'] ?? ''}',
      status: ContractStatus.fromServer(json['status']?.toString()),
      signedAt: json['signed_at']?.toString(),
      seal: json['seal']?.toString(),
    );
  }
}

/// Juego contractual tal como lo devuelve `GET .../contract`.
class ContractBundle {
  const ContractBundle({
    required this.applicationId,
    required this.status,
    required this.documents,
  });

  final String applicationId;
  final ContractStatus status;
  final List<ContractDocument> documents;

  bool get isEmpty => documents.isEmpty;
  bool get isSigned => status == ContractStatus.signed;

  factory ContractBundle.fromJson(Map<String, dynamic> json) {
    final raw = json['documents'];
    final docs = raw is List
        ? raw
            .whereType<Map>()
            .map((e) => ContractDocument.fromJson(
                  Map<String, dynamic>.from(e),
                ))
            .toList()
        : <ContractDocument>[];
    return ContractBundle(
      applicationId: '${json['application_id'] ?? ''}',
      status: ContractStatus.fromServer(json['status']?.toString()),
      documents: docs,
    );
  }

  /// Paquete vacio (sin documentos generados -> `EmptyView`).
  factory ContractBundle.empty({String applicationId = ''}) {
    return ContractBundle(
      applicationId: applicationId,
      status: ContractStatus.generated,
      documents: const [],
    );
  }
}

/// Reto de firma (`POST .../sign/challenge`, un solo uso, 120 s).
class SignChallenge {
  const SignChallenge({
    required this.applicationId,
    required this.nonce,
    required this.expiresIn,
    required this.status,
  });

  final String applicationId;
  final String nonce;
  final int expiresIn;
  final String status;

  bool get isSigned => status.toUpperCase() == 'SIGNED';

  factory SignChallenge.fromJson(Map<String, dynamic> json) {
    final expires = json['expires_in'];
    return SignChallenge(
      applicationId: '${json['application_id'] ?? ''}',
      nonce: '${json['nonce'] ?? ''}',
      expiresIn: expires is int
          ? expires
          : int.tryParse('${expires ?? 0}') ?? 0,
      status: '${json['status'] ?? ''}',
    );
  }
}

/// Desembolso automático informado en la respuesta de la firma (E3-T24,
/// HU12; bloque aditivo `disbursement`: `loan_id`, `transaction_id`,
/// `amount_minor`, `account_masked` y `disbursed_at`, o `status=FAILED`
/// con `code`/`message` cuando el automático no liquidó y queda el
/// reintento manual).
///
/// Cliente delgado: todo se muestra TAL CUAL lo devuelve el servidor.
class ContractDisbursement {
  const ContractDisbursement({
    required this.status,
    this.loanId,
    this.transactionId,
    this.amountMinor,
    this.accountMasked,
    this.disbursedAt,
    this.code,
    this.message,
  });

  final String status;
  final String? loanId;
  final String? transactionId;
  final int? amountMinor;
  final String? accountMasked;
  final String? disbursedAt;
  final String? code;
  final String? message;

  /// `true` solo si el servidor acreditó el desembolso automático.
  bool get isSettled => status.toUpperCase() == 'SETTLED';

  static int? _minorOf(Object? value) {
    if (value == null) return null;
    if (value is int) return value;
    if (value is num) return value.toInt();
    return int.tryParse('$value');
  }

  factory ContractDisbursement.fromJson(Map<String, dynamic> json) {
    return ContractDisbursement(
      status: '${json['status'] ?? ''}',
      loanId: json['loan_id']?.toString(),
      transactionId: json['transaction_id']?.toString(),
      amountMinor: _minorOf(json['amount_minor']),
      accountMasked: json['account_masked']?.toString(),
      disbursedAt: json['disbursed_at']?.toString(),
      code: json['code']?.toString(),
      message: json['message']?.toString(),
    );
  }
}

/// Resultado de `POST .../sign` (firma sellada + copia encolada +
/// desembolso automático E3-T24).
class ContractSignatureResult {
  const ContractSignatureResult({
    required this.applicationId,
    required this.contractId,
    required this.status,
    this.signedAt,
    this.via,
    this.documents = const [],
    this.replayed = false,
    this.disbursement,
  });

  final String applicationId;
  final String contractId;
  final String status;
  final String? signedAt;

  /// Via informada por el servidor (`device` / `pin`).
  final String? via;
  final List<ContractDocument> documents;
  final bool replayed;

  /// Desembolso automático (E3-T24; `null` en respuestas viejas).
  final ContractDisbursement? disbursement;

  bool get isSigned => status.toUpperCase() == 'SIGNED';

  /// `true` si el automático ya acreditó (hay confirmación que mostrar
  /// y la CTA manual no debe aparecer).
  bool get isAutoDisbursed => disbursement?.isSettled ?? false;

  factory ContractSignatureResult.fromJson(
    Map<String, dynamic> json, {
    bool replayed = false,
  }) {
    final raw = json['documents'];
    final docs = raw is List
        ? raw
            .whereType<Map>()
            .map((e) => ContractDocument.fromJson(
                  Map<String, dynamic>.from(e),
                ))
            .toList()
        : <ContractDocument>[];
    final rawDisbursement = json['disbursement'];
    return ContractSignatureResult(
      applicationId: '${json['application_id'] ?? ''}',
      contractId: '${json['contract_id'] ?? ''}',
      status: '${json['status'] ?? ''}',
      signedAt: json['signed_at']?.toString(),
      via: json['via']?.toString(),
      documents: docs,
      replayed: replayed,
      disbursement: rawDisbursement is Map
          ? ContractDisbursement.fromJson(
              Map<String, dynamic>.from(rawDisbursement),
            )
          : null,
    );
  }
}
