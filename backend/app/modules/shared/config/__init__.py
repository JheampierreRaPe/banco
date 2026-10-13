"""Fachada duena de `config.parameters` (E2-T18, HU08).

`shared/config` es la UNICA fuente duena del esquema y ciclo de vida de
`config.parameters` (`docs/modules/README.md#shared--config-infraestructura`);
`admin` solo la expone via `/admin/parameters` (E2-T26) y `transactions`/
`risk` la consumen por esta fachada (E2-T17/E2-T19). Ningun modulo lee la
tabla por su cuenta: toda lectura/escritura pasa por aqui.

Claves de esta tarea (regla de oro 6: regla variable = parametro):

- `transfer.biometric_threshold_minor` (default `1000000`): monto en
  centimos desde el cual el motor exige biometria.
- `transfer.daily_limit_minor` (default `500000`): limite diario generico
  en centimos (aplica si el perfil no tiene clave propia).
- `transfer.daily_limit_{PERFIL}_minor` (p. ej. `STANDARD`, default
  `500000`): limite diario por perfil de riesgo. El perfil `STANDARD`
  se siembra en la migracion `0023`; perfiles nuevos los siembra HU20
  (E6-T03/E6-T04) siguiendo el mismo patron (ver frontera abajo).
- `auth.max_failed_attempts` (default `5`): intentos fallidos antes del
  bloqueo temporal; la regla de bloqueo la aplica E2-T17.
- Motor de credito (E3-T07, HU10, modulo `credits`): `loan.max_dti_ratio`
  (`Decimal("0.4")`), `loan.max_amount_minor` (`5000000`),
  `loan.min_score` (`650`), `loan.review_margin` (`Decimal("50")`) y
  `loan.max_debt_minor` (`10000000`). Lectura con `get_loan_*` /
  `get_loan_params` (los ratios son `Decimal`, nunca `float`); escritura
  con `set_parameter` (acepta `Decimal`/`int`/`str` numerico en claves
  decimales y los guarda como numero JSON). Semillas: `0001`
  (`loan.max_dti_ratio`, como texto `"0.4"`, normalizado a numero por
  `0030`) y `0030` (el resto, idempotente).

Semantica:

- Lectura best-effort con fallback (patron `identity/service/
  otp_service.py`): si la tabla no existe, la clave falta o el valor
  almacenado es invalido, rige el default documentado y la lectura
  NUNCA lanza. Lectura por llamada, sin cache en proceso: un cambio
  (`set_parameter` o SQL directo) se refleja de inmediato, sin
  redespliegue ni reinicio.
- Escritura transaccional (`flush`, sin `commit`; decide el llamante),
  idempotente por `key` UQ (`UPDATE`: repetir no duplica filas;
  concurrencia = ultimo escritor gana, atomico por sentencia) y
  trazable (`updated_by` + `updated_at`, sin PII).
- `set_parameter` exige clave existente (`NOT_FOUND` si no hay fila):
  el espacio de claves lo gobiernan las migraciones; en caliente solo
  se editan valores. No se emite `parameter.updated`: se decidio no
  introducir el evento (ver reporte E2-T18; cambio pendiente en
  `docs/18`); la trazabilidad la dan `updated_by`/`updated_at`.

Frontera con HU20 (D-16): el motor antifraude completo (`risk_profiles`/
`alerts`, E6-T03/E6-T04, Sprint 4) es dueno de su esquema; aqui solo se
crea el minimo de `risk_profiles` que el motor consulta (columnas de
`docs/03b#112` ya fijadas) y las claves de limite por perfil. No se pisa
`alerts` ni `risk_rules`.
"""

from app.modules.shared.config.parameters import (
    AUTH_MAX_FAILED_ATTEMPTS_KEY,
    BIOMETRIC_THRESHOLD_KEY,
    DAILY_LIMIT_KEY,
    DAILY_LIMIT_PROFILE_TEMPLATE,
    DEFAULT_BIOMETRIC_THRESHOLD_MINOR,
    DEFAULT_DAILY_LIMIT_MINOR,
    DEFAULT_LOAN_MAX_AMOUNT_MINOR,
    DEFAULT_LOAN_MAX_DEBT_MINOR,
    DEFAULT_LOAN_MAX_DTI_RATIO,
    DEFAULT_LOAN_MIN_SCORE,
    DEFAULT_LOAN_REVIEW_MARGIN,
    DEFAULT_MAX_FAILED_ATTEMPTS,
    LOAN_MAX_AMOUNT_MINOR_KEY,
    LOAN_MAX_AMOUNT_PROFILE_TEMPLATE,
    LOAN_MAX_DEBT_MINOR_KEY,
    LOAN_MAX_DTI_RATIO_KEY,
    LOAN_MIN_SCORE_KEY,
    LOAN_REVIEW_MARGIN_KEY,
    SEEDED_PROFILES,
    ParameterError,
    ParameterNotFoundError,
    ParameterValidationError,
    daily_limit_key_for_profile,
    get_biometric_threshold_minor,
    get_daily_limit_minor,
    get_decimal_parameter,
    get_int_parameter,
    get_loan_max_amount_minor,
    get_loan_max_debt_minor,
    get_loan_max_dti_ratio,
    get_loan_min_score,
    get_loan_params,
    get_loan_review_margin,
    get_max_failed_attempts,
    get_parameter,
    loan_amount_key_for_profile,
    set_parameter,
)

__all__ = [
    "AUTH_MAX_FAILED_ATTEMPTS_KEY",
    "BIOMETRIC_THRESHOLD_KEY",
    "DAILY_LIMIT_KEY",
    "DAILY_LIMIT_PROFILE_TEMPLATE",
    "DEFAULT_BIOMETRIC_THRESHOLD_MINOR",
    "DEFAULT_DAILY_LIMIT_MINOR",
    "DEFAULT_LOAN_MAX_AMOUNT_MINOR",
    "DEFAULT_LOAN_MAX_DEBT_MINOR",
    "DEFAULT_LOAN_MAX_DTI_RATIO",
    "DEFAULT_LOAN_MIN_SCORE",
    "DEFAULT_LOAN_REVIEW_MARGIN",
    "DEFAULT_MAX_FAILED_ATTEMPTS",
    "LOAN_MAX_AMOUNT_MINOR_KEY",
    "LOAN_MAX_AMOUNT_PROFILE_TEMPLATE",
    "LOAN_MAX_DEBT_MINOR_KEY",
    "LOAN_MAX_DTI_RATIO_KEY",
    "LOAN_MIN_SCORE_KEY",
    "LOAN_REVIEW_MARGIN_KEY",
    "SEEDED_PROFILES",
    "ParameterError",
    "ParameterNotFoundError",
    "ParameterValidationError",
    "daily_limit_key_for_profile",
    "get_biometric_threshold_minor",
    "get_daily_limit_minor",
    "get_decimal_parameter",
    "get_int_parameter",
    "get_loan_max_amount_minor",
    "get_loan_max_debt_minor",
    "get_loan_max_dti_ratio",
    "get_loan_min_score",
    "get_loan_params",
    "get_loan_review_margin",
    "get_max_failed_attempts",
    "get_parameter",
    "loan_amount_key_for_profile",
    "set_parameter",
]
