/// Fuente unica de la version visible de la app (F-T30).
///
/// El valor se sobreescribe en build/run con
/// `--dart-define=APP_VERSION=<valor>`; si no se define, se usa el default
/// vigente del esquema de versionado documentado en
/// `docs/18-gestion-de-cambios.md` (seccion "Versionado visible de la app").
///
/// Correspondencia semver: `pubspec.yaml` usa `1.1.10+1` porque pub/semver no
/// acepta cero a la izquierda en el patch; el texto visible (el que identifica
/// el build) sigue siendo `1.1.010`, proveniente de [kAppVersion].
///
/// Es solo un valor de presentacion: no hay red, persistencia ni logica de
/// negocio asociada (cliente delgado, `docs/19-ejecucion-dos-campos.md`).
const String kAppVersion = String.fromEnvironment(
  'APP_VERSION',
  defaultValue: '1.1.010',
);
