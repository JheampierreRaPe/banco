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

  /// `POST /auth/kyc/submit` con la forma del proxy pre-registro:
  /// `{challenge_token, document: {type, image_b64}, segments: [{task, image_b64}]}`.
  ///
  /// [documentNumber] se registra en el controlador para la UX (pantalla de
  /// inicio) pero NO se transmite: el proxy pre-registro no expone campo para
  /// el numero (ver `backend/tests/test_kyc_proxy.py`). Si el contrato crece,
  /// agregarlo aqui sin tocar las pantallas.
  Future<KycSubmitResult> submit({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required Map<String, List<Uint8List>> framesByTask,
  });
}

/// Implementacion HTTP sobre [ApiClient] (capa de F-T01).
class HttpKycService implements KycService {
  HttpKycService(this._api);

  final ApiClient _api;

  /// Rutas relativas (el `baseUrl` de `ApiClient` ya incluye `/api/v1`).
  static const String challengePath = '/auth/kyc/challenge';
  static const String submitPath = '/auth/kyc/submit';

  @override
  Future<KycChallenge> challenge() async {
    // POST generico: KYC pre-registro no mueve dinero -> sin Idempotency-Key.
    final response = await _api.post(challengePath, data: const {});
    return KycChallenge.fromData(_dataOf(response.data));
  }

  @override
  Future<KycSubmitResult> submit({
    required String challengeToken,
    required String documentType,
    required String documentNumber,
    required Map<String, List<Uint8List>> framesByTask,
  }) {
    // documentNumber intencionalmente no enviado (ver contrato arriba).
    final usable = framesByTask.entries
        .where((e) => e.value.isNotEmpty)
        .toList(growable: false);
    if (usable.isEmpty) {
      throw ArgumentError('submit sin frames capturados', 'framesByTask');
    }
    final payload = {
      'challenge_token': challengeToken,
      'document': {
        'type': documentType,
        // Imagen del documento = primer frame de la primera tarea capturada.
        'image_b64': base64Encode(usable.first.value.first),
      },
      'segments': [
        for (final e in usable)
          {'task': e.key, 'image_b64': base64Encode(e.value.first)},
      ],
    };
    return _api
        .post(submitPath, data: payload)
        .then((response) => KycSubmitResult.fromData(_dataOf(response.data)));
  }

  /// Extrae el `data` del envelope docs/05 `{data, meta}`.
  static Map<String, dynamic> _dataOf(Object? body) {
    if (body is Map && body['data'] is Map) {
      return Map<String, dynamic>.from(body['data'] as Map);
    }
    throw const FormatException('Envelope KYC sin campo data');
  }
}
