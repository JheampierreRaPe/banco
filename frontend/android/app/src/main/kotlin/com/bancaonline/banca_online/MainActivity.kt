package com.bancaonline.banca_online

import io.flutter.embedding.android.FlutterFragmentActivity

// `FlutterFragmentActivity` (en vez de `FlutterActivity`): requisito de
// `local_auth` en Android para el prompt biometrico (F-T46).
class MainActivity : FlutterFragmentActivity()
