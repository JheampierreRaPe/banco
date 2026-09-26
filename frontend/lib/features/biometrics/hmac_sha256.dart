// HMAC-SHA256 en Dart puro (F-T03).
//
// `pubspec.yaml` esta congelado para esta tarea (lo decide el orquestador),
// asi que no se puede agregar `package:crypto`. Esta implementacion sigue
// FIPS 180-4 (SHA-256) + RFC 2104 (HMAC) sin dependencias externas y existe
// solo para firmar el `nonce` de login con el `device.secret` de F-T02.
//
// Verificado contra el vector conocido:
// HMAC-SHA256("key", "The quick brown fox jumps over the lazy dog") =
// f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8
// (ver `test/biometrics_service_test.dart`).
library;

/// SHA-256 de [message]. Devuelve 32 bytes.
List<int> sha256(List<int> message) {
  var h0 = 0x6a09e667;
  var h1 = 0xbb67ae85;
  var h2 = 0x3c6ef372;
  var h3 = 0xa54ff53a;
  var h4 = 0x510e527f;
  var h5 = 0x9b05688c;
  var h6 = 0x1f83d9ab;
  var h7 = 0x5be0cd19;

  final bytes = List<int>.from(message);
  final bitLength = bytes.length * 8;
  bytes.add(0x80);
  while (bytes.length % 64 != 56) {
    bytes.add(0x00);
  }
  for (var i = 7; i >= 0; i--) {
    bytes.add((bitLength >> (i * 8)) & 0xff);
  }

  const mask = 0xffffffff;
  for (var offset = 0; offset < bytes.length; offset += 64) {
    final w = List<int>.filled(64, 0);
    for (var i = 0; i < 16; i++) {
      w[i] = ((bytes[offset + i * 4] << 24) |
              (bytes[offset + i * 4 + 1] << 16) |
              (bytes[offset + i * 4 + 2] << 8) |
              bytes[offset + i * 4 + 3]) &
          mask;
    }
    for (var i = 16; i < 64; i++) {
      final s0 = (_rotr(w[i - 15], 7) ^
              _rotr(w[i - 15], 18) ^
              (w[i - 15] >> 3)) &
          mask;
      final s1 = (_rotr(w[i - 2], 17) ^
              _rotr(w[i - 2], 19) ^
              (w[i - 2] >> 10)) &
          mask;
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) & mask;
    }

    var a = h0, b = h1, c = h2, d = h3;
    var e = h4, f = h5, g = h6, h = h7;
    for (var i = 0; i < 64; i++) {
      final s1 = (_rotr(e, 6) ^ _rotr(e, 11) ^ _rotr(e, 25)) & mask;
      final ch = ((e & f) ^ ((~e) & g)) & mask;
      final t1 = (h + s1 + ch + _k[i] + w[i]) & mask;
      final s0 = (_rotr(a, 2) ^ _rotr(a, 13) ^ _rotr(a, 22)) & mask;
      final maj = ((a & b) ^ (a & c) ^ (b & c)) & mask;
      final t2 = (s0 + maj) & mask;
      h = g;
      g = f;
      f = e;
      e = (d + t1) & mask;
      d = c;
      c = b;
      b = a;
      a = (t1 + t2) & mask;
    }
    h0 = (h0 + a) & mask;
    h1 = (h1 + b) & mask;
    h2 = (h2 + c) & mask;
    h3 = (h3 + d) & mask;
    h4 = (h4 + e) & mask;
    h5 = (h5 + f) & mask;
    h6 = (h6 + g) & mask;
    h7 = (h7 + h) & mask;
  }

  final out = <int>[];
  for (final h in [h0, h1, h2, h3, h4, h5, h6, h7]) {
    out.addAll([(h >> 24) & 0xff, (h >> 16) & 0xff, (h >> 8) & 0xff, h & 0xff]);
  }
  return out;
}

int _rotr(int value, int shift) =>
    ((value >> shift) | (value << (32 - shift))) & 0xffffffff;

const List<int> _k = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
  0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
  0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
  0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
  0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
  0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

/// HMAC-SHA256 ([key], [message]) como hex en minusculas (64 chars).
///
/// Formato identico al que espera el backend (`backend/tests/test_device_login.py`:
/// `hmac.new(secret, nonce.encode("utf-8"), hashlib.sha256).hexdigest()`).
String hmacSha256Hex(List<int> key, List<int> message) {
  var k = List<int>.from(key);
  if (k.length > 64) k = sha256(k);
  while (k.length < 64) {
    k.add(0x00);
  }
  final ipad = List<int>.generate(64, (i) => k[i] ^ 0x36);
  final opad = List<int>.generate(64, (i) => k[i] ^ 0x5c);
  final inner = sha256([...ipad, ...message]);
  final outer = sha256([...opad, ...inner]);
  final buffer = StringBuffer();
  for (final byte in outer) {
    buffer.write(byte.toRadixString(16).padLeft(2, '0'));
  }
  return buffer.toString();
}
