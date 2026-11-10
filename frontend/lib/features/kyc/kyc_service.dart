import 'dart:convert';
import 'dart:typed_data';

import '../../core/http/api_client.dart';
import 'kyc_models.dart';

/// Contrato del flujo KYC (E1-T05).
///
/// - [challenge] pide el desafio (`token`, `steps`, `expires_in`).
/// - [submit] envia documento + segmentos y devuelve el `overall_result`.
///
/// Los frames viven SOLO en memoria (docs/13 §1.2: nunca en disco) y se
/// codifican a base64 aqui, durante el submit.
abstract class KycService {
  /// `POST /auth/kyc/challenge` con cuerpo vacio.
  Future<KycChallenge> challenge();

  /// `POST /auth/kyc/submit` con el contrato ampliado (E1-T24 / F-T19):
  /// `{challenge_token,
  ///   document: {type, number, image_b64},
  ///   applicant: {first_name, last_name, email, phone},
  ///   segments: [{task, frames_b64, image_b64}]}`.
  ///
  /// El cliente solo envia datos crudos; el backend valida y decide
  /// (cliente delgado, docs/19). [documentType] llega con el valor de la UI y
  /// se traduce al del contrato (`Pasaporte -> PASSPORT`).
  Future<KycSubmitResult> submit({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required KycApplicant applicant,
    required Map<String, List<Uint8List>> framesByTask,
  });
}

/// Capacidades ampliadas del KYC (E1-T29 / F-T23): evaluacion de un paso en
/// vivo y submit con la imagen real del documento capturada aparte.
///
/// Se declara como interfaz SEPARADA (no como miembros nuevos de [KycService])
/// para no romper los dobles de test existentes: [KycFlowController] la recibe
/// opcional y, en produccion, [HttpKycService] la implementa. `HttpKycService`
/// cumple [KycService] y [KycEvaluationService] a la vez.
abstract class KycEvaluationService {
  /// `POST /auth/kyc/evaluate` (E1-T29): manda la ráfaga completa del paso y
  /// devuelve `passed`/`reason` reales.
  Future<KycTaskEvaluation> evaluate({
    required String challengeToken,
    required String step,
    required List<Uint8List> frames,
  });

  /// `POST /auth/kyc/submit` con `frames_b64` (todos los frames) por segmento
  /// y la imagen real del documento en `document.image_b64`.
  Future<KycSubmitResult> submitWithDocument({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required KycApplicant applicant,
    required Map<String, List<Uint8List>> framesByTask,
    Uint8List? documentImage,
  });

  /// `POST /auth/kyc/document/validate` (E1-T30 / F-T26): manda la foto del
  /// documento en base64 y devuelve la decision del servidor (`is_valid` +
  /// `issues`). `is_valid=false` NO es error HTTP: es un 200 con los motivos
  /// (cliente delgado, docs/19). La imagen no se persiste ni se registra.
  Future<KycDocumentValidation> validateDocument({required Uint8List image});
}

/// Consulta del titular por documento (E1-T35 / F-T44).
///
/// Se declara como interfaz SEPARADA (no como miembros nuevos de [KycService])
/// para no romper los dobles de test existentes: la pantalla de inicio la
/// recibe opcional y, en produccion, [HttpKycService] la implementa.
/// `HttpKycService` cumple [KycService], [KycEvaluationService] y
/// [KycDocumentLookupService] a la vez.
///
/// Cliente delgado (docs/19 §4): solo llama al backend
/// (`POST /auth/kyc/document/lookup`); jamas a la API externa ni conoce su
/// key. Sin PII en logs: no se registra el numero ni la respuesta.
abstract class KycDocumentLookupService {
  /// `POST /auth/kyc/document/lookup` (E1-T35): `{type, number}` con el tipo
  /// ya mapeado por [mapDocumentTypeToApi] y devuelve el titular normalizado
  /// ([KycDocumentOwner]): persona natural (`first_name`/`last_name`) o RUC
  /// de persona juridica (`business_name`).
  Future<KycDocumentOwner> lookupDocument({
    required String type,
    required String number,
  });
}

/// Prechequeo de email (E1-T40 / F-T50).
///
/// Se declara como interfaz SEPARADA (no como miembros nuevos de [KycService])
/// para no romper los dobles de test existentes: la pantalla de inicio la
/// recibe opcional y, en produccion, [HttpKycService] la implementa.
///
/// Cliente delgado (docs/19 §4): solo llama al backend
/// (`POST /auth/kyc/email/check`); la existencia la decide el servidor.
/// Sin PII en logs: no se registra el email.
abstract class KycEmailCheckService {
  /// `POST /auth/kyc/email/check` (E1-T40): `{email}`.
  ///
  /// `200 {available: true}` si no existe (retorna sin error);
  /// `409 DUPLICATE_EMAIL` si existe (lanza [ApiException] con ese codigo
  /// via la capa HTTP; 422/429/red se propagan igual).
  Future<void> checkEmail({required String email});
}

/// Implementacion HTTP sobre [ApiClient] (capa de F-T01).
class HttpKycService
    implements
        KycService,
        KycEvaluationService,
        KycDocumentLookupService,
        KycEmailCheckService {
  HttpKycService(this._api);

  final ApiClient _api;

  /// Rutas relativas (el `baseUrl` de `ApiClient` ya incluye `/api/v1`).
  static const String challengePath = '/auth/kyc/challenge';
  static const String evaluatePath = '/auth/kyc/evaluate';
  static const String submitPath = '/auth/kyc/submit';
  static const String documentValidatePath = '/auth/kyc/document/validate';

  /// Ruta de consulta del titular por documento (E1-T35 / F-T44).
  static const String documentLookupPath = '/auth/kyc/document/lookup';

  /// Ruta de prechequeo de email (E1-T40 / F-T50).
  static const String emailCheckPath = '/auth/kyc/email/check';

  @override
  Future<KycChallenge> challenge() async {
    // POST generico: KYC pre-registro no mueve dinero -> sin Idempotency-Key.
    final response = await _api.post(challengePath, data: const {});
    return KycChallenge.fromData(_dataOf(response.data));
  }

  @override
  Future<KycTaskEvaluation> evaluate({
    required String challengeToken,
    required String step,
    required List<Uint8List> frames,
  }) async {
    if (frames.isEmpty) {
      throw ArgumentError('evaluate sin frames capturados', 'frames');
    }
    final payload = {
      'challenge_token': challengeToken,
      'step': step,
      // Ráfaga completa, en orden; el microservicio exige >=5 por tarea.
      'frames_b64': [for (final frame in frames) base64Encode(frame)],
    };
    final response = await _api.post(evaluatePath, data: payload);
    return KycTaskEvaluation.fromData(
      _dataOf(response.data),
      fallbackStep: step,
    );
  }

  @override
  Future<KycSubmitResult> submit({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required KycApplicant applicant,
    required Map<String, List<Uint8List>> framesByTask,
  }) =>
      submitWithDocument(
        challengeToken: challengeToken,
        documentType: documentType,
        documentNumber: documentNumber,
        applicant: applicant,
        framesByTask: framesByTask,
      );

  @override
  Future<KycSubmitResult> submitWithDocument({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required KycApplicant applicant,
    required Map<String, List<Uint8List>> framesByTask,
    Uint8List? documentImage,
  }) {
    final usable = framesByTask.entries
        .where((e) => e.value.isNotEmpty)
        .toList(growable: false);
    if (usable.isEmpty) {
      throw ArgumentError('submit sin frames capturados', 'framesByTask');
    }
    // Imagen del documento real (cámara trasera, F-T23); fallback legacy al
    // primer frame de liveness cuando no se capturó aparte.
    final documentBytes = documentImage ?? usable.first.value.first;
    final payload = {
      'challenge_token': challengeToken,
      'document': {
        'type': mapDocumentTypeToApi(documentType),
        'number': documentNumber,
        'image_b64': base64Encode(documentBytes),
      },
      'applicant': applicant.toJson(),
      'segments': [
        for (final e in usable)
          {
            'task': e.key,
            // Ráfaga completa por segmento (E1-T29); el backend exige >=5.
            // `frames_b64` es la fuente unica: NO se envia tambien `image_b64`
            // con el primer frame (el adapter lo concatenaria y lo duplicaria).
            'frames_b64': [
              for (final frame in e.value) base64Encode(frame),
            ],
          },
      ],
    };
    return _api
        .post(submitPath, data: payload)
        .then((response) => KycSubmitResult.fromData(_dataOf(response.data)));
  }

  @override
  Future<KycDocumentValidation> validateDocument({
    required Uint8List image,
  }) async {
    // La imagen viaja en base64, igual que evaluate/submit; el backend la
    // reenvia como multipart al microservicio. Solo datos crudos: la decision
    // es del servidor (cliente delgado, docs/19).
    final payload = {'image_b64': base64Encode(image)};
    final response = await _api.post(documentValidatePath, data: payload);
    return KycDocumentValidation.fromData(_dataOf(response.data));
  }

  @override
  Future<KycDocumentOwner> lookupDocument({
    required String type,
    required String number,
  }) async {
    // Solo datos crudos al backend (E1-T35); el servidor consulta al
    // proveedor y normaliza. La key externa nunca esta en el cliente.
    final payload = {
      'type': mapDocumentTypeToApi(type),
      'number': number,
    };
    final response = await _api.post(documentLookupPath, data: payload);
    return KycDocumentOwner.fromData(_dataOf(response.data));
  }

  @override
  Future<void> checkEmail({required String email}) async {
    // Solo el email al backend (E1-T40); el servidor decide la existencia
    // (cliente delgado). El 409 `DUPLICATE_EMAIL` lo convierte la capa HTTP
    // en `ApiException` con ese codigo; aqui no se registra el email.
    await _api.post(emailCheckPath, data: {'email': email});
  }

  /// Extrae el `data` del envelope docs/05 `{data, meta}`.
  static Map<String, dynamic> _dataOf(Object? body) {
    if (body is Map && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    throw const FormatException('Envelope KYC sin campo data');
  }
}
