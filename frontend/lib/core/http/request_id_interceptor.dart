import 'package:dio/dio.dart';
import 'package:uuid/uuid.dart';

/// Agrega `X-Request-Id` (uuid v4) a cada request si aun no existe.
///
/// Sirve para correlacion de logs (docs/05 §3). Si el backend no lo recibe,
/// lo genera; el cliente siempre lo envia.
class RequestIdInterceptor extends Interceptor {
  RequestIdInterceptor({Uuid? uuid}) : _uuid = uuid ?? const Uuid();

  final Uuid _uuid;

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    options.headers.putIfAbsent('X-Request-Id', () => _uuid.v4());
    handler.next(options);
  }
}
