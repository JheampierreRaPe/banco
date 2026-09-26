import '../../../core/http/api_client.dart';

/// Nombre del titular para el saludo/avatar del `dash` (E1-T44, F-T54).
///
/// Refleja `GET /api/v1/me` -> `data.{first_name, last_name, business_name}`.
/// El nombre **no** se persiste localmente: solo se muestra. Las iniciales
/// derivadas son presentacion aceptable (no es calculo de saldos).
class Profile {
  const Profile({
    required this.firstName,
    required this.lastName,
    this.businessName,
  });

  final String firstName;
  final String lastName;
  final String? businessName;

  /// Nombre a mostrar: razon social si esta presente, si no `nombre apellido`.
  String get displayName {
    final business = (businessName ?? '').trim();
    if (business.isNotEmpty) return business;
    return '$firstName $lastName'.trim();
  }

  /// Primer nombre (o razon social) para el saludo `Hola, {nombre}`.
  String get shortName {
    final business = (businessName ?? '').trim();
    if (business.isNotEmpty) return business;
    final first = firstName.trim();
    if (first.isNotEmpty) return first;
    return lastName.trim();
  }

  /// Iniciales del avatar (p. ej. "Ana Lopez" -> "AL").
  String get initials {
    final words = displayName
        .split(RegExp(r'\s+'))
        .where((w) => w.isNotEmpty)
        .toList();
    if (words.isEmpty) return '';
    final first = words.first[0];
    final second = words.length > 1 ? words[1][0] : '';
    return '$first$second'.toUpperCase();
  }

  factory Profile.fromJson(Map<String, dynamic> json) {
    return Profile(
      firstName: json['first_name']?.toString() ?? '',
      lastName: json['last_name']?.toString() ?? '',
      businessName: json['business_name']?.toString(),
    );
  }
}

/// Contrato del servicio de perfil (seam para tests con fake).
abstract class ProfileServiceBase {
  Future<Profile> getProfile();
}

/// Servicio de perfil sobre [ApiClient] (E1-T44).
///
/// Endpoint (base `/api/v1` ya incluida en el cliente):
/// - `GET /me` -> `{"data": {"first_name", "last_name", "business_name"}}`
///
/// Solo GET de lectura propia; no mueve dinero, no requiere `Idempotency-Key`.
class HttpProfileService implements ProfileServiceBase {
  HttpProfileService({required this.api});

  final ApiClient api;

  @override
  Future<Profile> getProfile() async {
    final resp = await api.get('/me');
    final data = resp.data;
    if (data is Map<String, dynamic> && data['data'] is Map) {
      return Profile.fromJson(Map<String, dynamic>.from(data['data'] as Map));
    }
    throw const FormatException('Respuesta de perfil invalida');
  }
}

/// Fabrica inyectable del servicio de perfil (F-T54).
///
/// El orquestador `core/router/app_router.dart` la asigna una sola vez con el
/// `ApiClient` compartido (mismo patron que `accountsServiceFactory`);
/// `null` = DI no configurada: la topbar usa el fallback generico "Hola".
typedef HomeProfileServiceFactory = ProfileServiceBase Function();

HomeProfileServiceFactory? homeProfileServiceFactory;
