# 03b - Diccionario de datos (columna por columna)

> Companero de `03-modelo-de-datos.md`. Define el detalle ejecutable de cada tabla: tipo, nulo,
> default, claves, restricciones e indices. Es la fuente para generar migraciones Alembic.
>
> Regla de modularidad: **no existen FK entre schemas distintos**. Las referencias cruzadas se
> marcan como `REF` (referencia logica por UUID, sin constraint de base de datos).

## 0. Convenciones y leyenda

- Todo `id` es `UUID PK DEFAULT gen_random_uuid()`.
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` (trigger en UPDATE).
- Dinero: `*_minor BIGINT` + `currency CHAR(3)` (ISO-4217). Nunca decimal flotante.
- Porcentajes/tasas: `NUMERIC(18,8)`.
- Borrado: no se elimina en tablas financieras/auditoria; se usa `status`.
- Convencion de estados en `MAYUSCULAS`.

| Marca | Significado |
|---|---|
| PK | Clave primaria |
| FK | Clave foranea **dentro del mismo schema** |
| REF | Referencia logica a otro schema (sin FK fisica) |
| UQ | Unico |
| CK | Check constraint |
| IDX | Indice recomendado |

## 1. Catalogo de enums (valores permitidos)

| Enum | Valores |
|---|---|
| `user_status` | `PENDING_ACTIVATION`, `ACTIVE`, `BLOCKED`, `CLOSED` |
| `kyc_status` | `PENDING`, `VERIFIED`, `REJECTED`, `MANUAL_REVIEW` |
| `doc_type` | `DNI`, `CE`, `PASSPORT` |
| `credential_type` | `PIN`, `PASSWORD` |
| `otp_purpose` | `ACTIVATION`, `RECOVERY`, `PAYMENT`, `LOGIN` |
| `account_type` | `AHORRO`, `CORRIENTE`, `WALLET`, `POCKET`, `MULTICURRENCY` |
| `account_status` | `ACTIVE`, `BLOCKED`, `CLOSED` |
| `transaction_type` | `OWN_TRANSFER`, `THIRD_PARTY_TRANSFER`, `INTERBANK_TRANSFER`, `QR_PAYMENT`, `SERVICE_PAYMENT`, `TOPUP`, `LOAN_DISBURSEMENT`, `LOAN_INSTALLMENT`, `FX_EXCHANGE`, `POCKET_TRANSFER`, `SALARY_DISTRIBUTION`, `REVERSAL` |
| `transaction_status` | `INITIATED`, `VALIDATED`, `PENDING_AUTHORIZATION`, `AUTHORIZED`, `FUNDS_HELD`, `POSTED`, `SETTLED`, `CONCILIATED`, `REJECTED`, `FAILED`, `REVERSED` |
| `hold_status` | `ACTIVE`, `RELEASED`, `CAPTURED`, `EXPIRED` |
| `ledger_account_type` | `ASSET`, `LIABILITY`, `EQUITY`, `INCOME`, `EXPENSE` |
| `posting_direction` | `DEBIT`, `CREDIT` |
| `journal_status` | `POSTED`, `REVERSED` |
| `loan_application_status` | `DRAFT`, `SUBMITTED`, `IN_EVALUATION`, `APPROVED`, `REJECTED`, `IN_REVIEW`, `EXPIRED` |
| `loan_status` | `ACTIVE`, `PAID`, `DEFAULTED`, `WRITTEN_OFF` |
| `installment_status` | `PENDING`, `PARTIAL`, `PAID`, `OVERDUE` |
| `contract_status` | `GENERATED`, `SIGNED`, `VOID` |
| `qr_charge_status` | `ACTIVE`, `PAID`, `EXPIRED`, `CANCELLED` |
| `fx_quote_status` | `ACTIVE`, `EXECUTED`, `EXPIRED`, `CANCELLED` |
| `pocket_movement_type` | `TRANSFER`, `ROUNDUP`, `YIELD`, `WITHDRAWAL` |
| `risk_action` | `ALERT`, `HOLD`, `BLOCK` |
| `alert_severity` | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `alert_status` | `OPEN`, `IN_REVIEW`, `RESOLVED`, `DISMISSED` |
| `screening_subject` | `USER`, `TRANSACTION` |
| `ros_status` | `DRAFT`, `FILED`, `CLOSED` |
| `blocked_entity_type` | `USER`, `ACCOUNT`, `DEVICE`, `IP` |
| `clearing_status` | `RECEIVED`, `PROCESSING`, `PROCESSED`, `FAILED` |
| `clearing_item_status` | `PENDING`, `MATCHED`, `EXCEPTION` |
| `exception_type` | `MISSING_INTERNAL`, `MISSING_EXTERNAL`, `AMOUNT_MISMATCH`, `DATE_MISMATCH`, `DUPLICATE` |
| `claim_status` | `OPEN`, `IN_REVIEW`, `RESOLVED`, `REJECTED` |
| `notification_channel` | `PUSH`, `EMAIL`, `SMS` |
| `notification_status` | `QUEUED`, `SENT`, `FAILED`, `READ` |
| `outbox_status` | `PENDING`, `PUBLISHED`, `FAILED` |

## 2. Schema `shared` (transversal)

### 2.1 `idempotency_keys`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| key | VARCHAR(80) | No | - | UQ(key,user_id) | Clave enviada por el cliente. |
| user_id | UUID | Si | - | REF identity.users | Null para servicios tecnicos. |
| endpoint | VARCHAR(150) | No | - | | Ruta destino. |
| method | VARCHAR(10) | No | - | | Verbo HTTP. |
| request_hash | VARCHAR(64) | No | - | | SHA-256 del cuerpo. |
| response_snapshot | JSONB | Si | - | | Respuesta original para repetir. |
| transaction_id | UUID | Si | - | REF transactions.transactions | Operacion asociada. |
| created_at | TIMESTAMPTZ | No | now() | IDX | - |
| expires_at | TIMESTAMPTZ | No | - | IDX | TTL de la clave. |

### 2.2 `outbox`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| event_type | VARCHAR(80) | No | - | IDX | Ej. `kyc.completed`. |
| aggregate_type | VARCHAR(60) | No | - | | Entidad origen. |
| aggregate_id | UUID | No | - | IDX | ID del agregado. |
| payload | JSONB | No | - | | Contenido del evento. |
| status | VARCHAR(12) | No | 'PENDING' | IDX | `outbox_status`. |
| attempts | SMALLINT | No | 0 | | Reintentos. |
| available_at | TIMESTAMPTZ | No | now() | IDX | Backoff. |
| created_at | TIMESTAMPTZ | No | now() | | - |
| published_at | TIMESTAMPTZ | Si | - | | - |

### 2.3 `processed_events` (inbox)

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| event_id | UUID | No | - | PK(con consumer) | Evento procesado. |
| consumer | VARCHAR(60) | No | - | PK(con event_id) | Modulo/consumidor. |
| processed_at | TIMESTAMPTZ | No | now() | | - |

## 3. Schema `config`

### 3.1 `parameters`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| key | VARCHAR(100) | No | - | UQ | Ej. `transfer.biometric_threshold_minor`. |
| value_json | JSONB | No | - | | Valor tipado. |
| module | VARCHAR(40) | No | - | IDX | Modulo dueno. |
| description | TEXT | Si | - | | Explicacion del parametro. |
| updated_by | UUID | Si | - | REF identity.users | - |
| created_at | TIMESTAMPTZ | No | now() | | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |

**Parametros semilla sugeridos:** `transfer.biometric_threshold_minor`,
`transfer.daily_limit_minor`, `qr.express_limit_minor`, `otp.ttl_seconds`,
`otp.max_resends`, `auth.max_failed_attempts`, `session.inactivity_seconds`,
`kyc.match_threshold`, `kyc.max_attempts`, `loan.max_dti_ratio`,
`fx.quote_ttl_seconds`, `fx.spread_base`, `fx.spread_volume_step`, `pocket.yield_rate`.

## 4. Schema `identity`

### 4.1 `users`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| doc_type | VARCHAR(10) | No | - | CK `doc_type` | Tipo de documento. |
| doc_number_hash | VARCHAR(128) | No | - | UQ | Hash del numero (no claro). |
| doc_number_masked | VARCHAR(20) | Si | - | | Version enmascarada. |
| first_name | VARCHAR(100) | No | - | | - |
| last_name | VARCHAR(100) | No | - | | - |
| birth_date | DATE | Si | - | | - |
| email | CITEXT | Si | - | UQ | - |
| phone | VARCHAR(20) | Si | - | IDX | - |
| status | VARCHAR(20) | No | 'PENDING_ACTIVATION' | IDX, CK | `user_status`. |
| kyc_status | VARCHAR(20) | No | 'PENDING' | IDX | `kyc_status`. |
| risk_profile | VARCHAR(20) | No | 'STANDARD' | | Perfil de riesgo. |
| created_at | TIMESTAMPTZ | No | now() | | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |

### 4.2 `credentials`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| user_id | UUID | No | - | PK, FK users | 1:1 con usuario. |
| password_hash | TEXT | Si | - | | Argon2/bcrypt. |
| pin_hash | TEXT | Si | - | | PIN de contingencia. |
| biometric_enabled | BOOLEAN | No | false | | Habilitado en dispositivos. |
| failed_attempts | SMALLINT | No | 0 | CK >=0 | Contador de fallos. |
| locked_until | TIMESTAMPTZ | Si | - | | Bloqueo temporal. |
| password_updated_at | TIMESTAMPTZ | Si | - | | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |

### 4.3 `device_bindings`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| user_id | UUID | No | - | FK users, IDX | - |
| device_id | VARCHAR(128) | No | - | UQ(user_id,device_id) | Identificador del dispositivo. |
| public_key | TEXT | No | - | | Clave publica para firmar el `nonce`. |
| platform | VARCHAR(20) | Si | - | | `android`/`ios`. |
| biometric_type | VARCHAR(20) | Si | - | | `FACE`/`FINGERPRINT`. |
| status | VARCHAR(15) | No | 'ACTIVE' | | `ACTIVE`/`REVOKED`. |
| registered_at | TIMESTAMPTZ | No | now() | | - |
| last_used_at | TIMESTAMPTZ | Si | - | | - |

### 4.4 `kyc_verifications`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| user_id | UUID | Si | - | FK users, IDX | Null si aun no existe usuario. |
| provider | VARCHAR(50) | No | - | | `facial-kyc-service`. |
| overall_result | BOOLEAN | No | - | | Resultado final. |
| document_json | JSONB | Si | - | | Resultado de validacion de documento. |
| liveness_json | JSONB | Si | - | | Resultado de liveness (sin frames). |
| face_match_json | JSONB | Si | - | | Distancia/umbral/confianza. |
| challenge_token_hash | VARCHAR(128) | Si | - | | Token del desafio (hasheado). |
| created_at | TIMESTAMPTZ | No | now() | IDX | - |

> Prohibido persistir frames base64 o imagenes biometricas.

### 4.5 `otp_codes`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| user_id | UUID | No | - | FK users, IDX | - |
| purpose | VARCHAR(30) | No | - | CK | `otp_purpose`. |
| destination | VARCHAR(255) | No | - | | Correo o celular. |
| code_hash | VARCHAR(128) | No | - | | Hash del OTP. |
| expires_at | TIMESTAMPTZ | No | - | IDX | 10 min por defecto. |
| attempts | SMALLINT | No | 0 | | Intentos de ingreso. |
| max_attempts | SMALLINT | No | 3 | | Limite. |
| status | VARCHAR(20) | No | 'PENDING' | IDX | `PENDING`/`USED`/`EXPIRED`. |
| created_at | TIMESTAMPTZ | No | now() | | - |
| consumed_at | TIMESTAMPTZ | Si | - | | - |

### 4.6 `sessions`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| user_id | UUID | No | - | FK users, IDX | - |
| refresh_token_hash | VARCHAR(128) | No | - | UQ | Rotativo. |
| device_id | VARCHAR(128) | Si | - | IDX | - |
| device_info | JSONB | Si | - | | Modelo/SO. |
| ip | INET | Si | - | | - |
| expires_at | TIMESTAMPTZ | No | - | IDX | - |
| revoked_at | TIMESTAMPTZ | Si | - | | Revocacion. |
| created_at | TIMESTAMPTZ | No | now() | | - |

### 4.7 `roles` y `user_roles`

| Tabla | Columna | Tipo | Nulo | Default | Clave |
|---|---|---|---|---|---|
| roles | id | UUID | No | gen_random_uuid() | PK |
| roles | code | VARCHAR(50) | No | - | UQ |
| roles | name | VARCHAR(80) | No | - | |
| roles | description | TEXT | Si | - | |
| user_roles | user_id | UUID | No | - | PK, FK users |
| user_roles | role_id | UUID | No | - | PK, FK roles |
| user_roles | assigned_by | UUID | Si | - | REF users |
| user_roles | assigned_at | TIMESTAMPTZ | No | now() | |

**Roles semilla:** `CLIENT`, `MERCHANT`, `OPS_ANALYST`, `FRAUD_ANALYST`, `CREDIT_ANALYST`,
`COMPLIANCE_OFFICER`, `SECURITY_ADMIN`, `AUDITOR`, `TRANSACTION_SERVICE`.

### 4.8 `access_recovery`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| user_id | UUID | No | - | FK users, IDX | - |
| method | VARCHAR(30) | No | - | | `DEVICE_BIOMETRIC`/`OTP`. |
| verification_result | JSONB | Si | - | | Evidencia de la verificacion. |
| device_id | VARCHAR(128) | Si | - | | Dispositivo usado. |
| new_credential_set | BOOLEAN | No | false | | Credencial restablecida. |
| notified_channels | JSONB | Si | - | | Canales notificados. |
| created_at | TIMESTAMPTZ | No | now() | | - |

## 5. Schema `accounts`

### 5.1 `accounts`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| user_id | UUID | No | - | REF identity.users, IDX | - |
| account_number | VARCHAR(20) | No | - | UQ | Numero interno. |
| type | VARCHAR(20) | No | - | CK | `account_type`. |
| currency | CHAR(3) | No | 'PEN' | | ISO-4217. |
| status | VARCHAR(20) | No | 'ACTIVE' | IDX, CK | `account_status`. |
| ledger_account_id | UUID | Si | - | REF ledger.ledger_accounts | Cuenta contable (disponible). |
| ledger_hold_account_id | UUID | Si | - | REF ledger.ledger_accounts | Subcuenta retenido. |
| opened_at | TIMESTAMPTZ | No | now() | | - |
| created_at | TIMESTAMPTZ | No | now() | | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |

### 5.2 `account_balances`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| account_id | UUID | No | - | PK, FK accounts | 1:1. |
| currency | CHAR(3) | No | - | | - |
| available_minor | BIGINT | No | 0 | CK >=0 | Disponible. |
| held_minor | BIGINT | No | 0 | CK >=0 | Retenido. |
| version | INTEGER | No | 0 | | Optimistic lock. |
| updated_at | TIMESTAMPTZ | No | now() | | - |

### 5.3 `beneficiaries`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| owner_user_id | UUID | No | - | REF identity.users, IDX | - |
| alias | VARCHAR(80) | No | - | IDX | Nombre corto. |
| photo_url | TEXT | Si | - | | - |
| bank_code | VARCHAR(20) | Si | - | | - |
| account_or_cci | VARCHAR(30) | No | - | | Cuenta o CCI. |
| holder_name | VARCHAR(150) | Si | - | | - |
| holder_doc_hash | VARCHAR(128) | Si | - | | - |
| preapproved_limit_minor | BIGINT | Si | - | CK >=0 | Tope sin factor extra. |
| currency | CHAR(3) | Si | - | | - |
| status | VARCHAR(20) | No | 'ACTIVE' | | `ACTIVE`/`DELETED`. |
| created_at | TIMESTAMPTZ | No | now() | | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |

### 5.4 `movements_view` (vista materializada)

| Columna | Tipo | Descripcion |
|---|---|---|
| account_id | UUID | Cuenta. |
| journal_entry_id | UUID | Asiento. |
| transaction_id | UUID | Operacion. |
| direction | VARCHAR(6) | `DEBIT`/`CREDIT`. |
| amount_minor | BIGINT | Monto. |
| currency | CHAR(3) | Moneda. |
| description | TEXT | Concepto. |
| value_date | DATE | Fecha valor. |
| created_at | TIMESTAMPTZ | - |

Indices: `(account_id, created_at DESC)`.

### 5.5 `daily_balance_snapshots`

| Columna | Tipo | Nulo | Clave | Descripcion |
|---|---|---|---|---|
| account_id | UUID | No | PK(con snapshot_date) | Cuenta. |
| snapshot_date | DATE | No | PK(con account_id) | Corte diario. |
| available_minor | BIGINT | No | | Disponible. |
| held_minor | BIGINT | No | | Retenido. |

## 6. Schema `transactions`

### 6.1 `transactions`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| type | VARCHAR(30) | No | - | CK, IDX | `transaction_type`. |
| status | VARCHAR(25) | No | 'INITIATED' | IDX, CK | `transaction_status`. |
| idempotency_key | VARCHAR(80) | Si | - | IDX | - |
| initiator_user_id | UUID | Si | - | REF users, IDX | - |
| source_account_id | UUID | Si | - | REF accounts, IDX | - |
| target_account_id | UUID | Si | - | REF accounts | - |
| external_ref | VARCHAR(80) | Si | - | IDX | Referencia interbancaria/QR. |
| amount_minor | BIGINT | No | - | CK >0 | Monto. |
| currency | CHAR(3) | No | - | | Moneda. |
| fee_minor | BIGINT | No | 0 | CK >=0 | Comision. |
| risk_level | VARCHAR(15) | No | 'LOW' | | `LOW`/`MEDIUM`/`HIGH`/`CRITICAL`. |
| risk_score | SMALLINT | Si | - | | Puntaje. |
| failure_reason | TEXT | Si | - | | Motivo de rechazo/fallo. |
| metadata | JSONB | Si | - | | Datos adicionales. |
| version | INTEGER | No | 0 | | Optimistic lock. |
| created_at | TIMESTAMPTZ | No | now() | IDX | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |
| settled_at | TIMESTAMPTZ | Si | - | | - |

### 6.2 `transaction_status_history`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| transaction_id | UUID | No | - | FK transactions, IDX | - |
| from_status | VARCHAR(25) | Si | - | | Estado previo. |
| to_status | VARCHAR(25) | No | - | | Estado nuevo. |
| reason | TEXT | Si | - | | - |
| actor_type | VARCHAR(20) | No | 'SYSTEM' | | `SYSTEM`/`USER`/`ANALYST`. |
| actor_id | UUID | Si | - | | - |
| created_at | TIMESTAMPTZ | No | now() | IDX | - |

### 6.3 `holds`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| transaction_id | UUID | No | - | FK transactions, IDX | - |
| account_id | UUID | No | - | REF accounts, IDX | - |
| amount_minor | BIGINT | No | - | CK >0 | Retenido. |
| currency | CHAR(3) | No | - | | Moneda. |
| status | VARCHAR(15) | No | 'ACTIVE' | IDX, CK | `hold_status`. |
| held_at | TIMESTAMPTZ | No | now() | | - |
| expires_at | TIMESTAMPTZ | Si | - | IDX | - |
| released_at | TIMESTAMPTZ | Si | - | | - |

## 7. Schema `ledger`

### 7.1 `ledger_accounts`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| code | VARCHAR(30) | No | - | UQ | Ej. `2000-<uuid>`. |
| name | VARCHAR(120) | No | - | | - |
| type | VARCHAR(12) | No | - | CK, IDX | `ledger_account_type`. |
| currency | CHAR(3) | No | - | | Moneda. |
| owner_type | VARCHAR(20) | No | 'SYSTEM' | | `CUSTOMER`/`SYSTEM`/`MERCHANT`/`POOL`. |
| owner_ref | UUID | Si | - | IDX | Cuenta/usuario dueno. |
| parent_account_id | UUID | Si | - | FK ledger_accounts | Jerarquia. |
| is_system | BOOLEAN | No | false | | Cuenta del catalogo. |
| created_at | TIMESTAMPTZ | No | now() | | - |

### 7.2 `journal_entries`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| transaction_id | UUID | Si | - | REF transactions, IDX | Operacion origen. |
| entry_type | VARCHAR(30) | No | - | | Concepto contable. |
| description | TEXT | Si | - | | - |
| value_date | DATE | No | - | IDX | Fecha valor. |
| status | VARCHAR(12) | No | 'POSTED' | IDX, CK | `journal_status`. |
| reverses_entry_id | UUID | Si | - | FK journal_entries | Asiento revertido. |
| prev_hash | VARCHAR(64) | Si | - | | Encadenamiento. |
| hash | VARCHAR(64) | No | - | | Hash del asiento. |
| created_at | TIMESTAMPTZ | No | now() | IDX | - |

### 7.3 `postings` (append-only)

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| journal_entry_id | UUID | No | - | FK journal_entries, IDX | - |
| ledger_account_id | UUID | No | - | FK ledger_accounts, IDX | - |
| direction | VARCHAR(6) | No | - | CK | `posting_direction`. |
| amount_minor | BIGINT | No | - | CK >0 | Monto. |
| currency | CHAR(3) | No | - | | Moneda. |
| account_ref | UUID | Si | - | IDX | Cuenta de cliente (logica). |
| created_at | TIMESTAMPTZ | No | now() | IDX | - |

> Sin `UPDATE` ni `DELETE`. Regla: por asiento, `SUM(debit)=SUM(credit)` por moneda.

### 7.4 `ledger_balances`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| ledger_account_id | UUID | No | - | PK, FK ledger_accounts | 1:1. |
| currency | CHAR(3) | No | - | | Moneda. |
| balance_minor | BIGINT | No | 0 | | Saldo firmado. |
| version | INTEGER | No | 0 | | Optimistic lock. |
| updated_at | TIMESTAMPTZ | No | now() | | - |

### 7.5 `daily_closings`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| closing_date | DATE | No | - | UQ(con currency) | Fecha de cierre. |
| currency | CHAR(3) | No | - | UQ(con closing_date) | Moneda. |
| total_debits_minor | BIGINT | No | 0 | | Suma debitos. |
| total_credits_minor | BIGINT | No | 0 | | Suma creditos. |
| balanced | BOOLEAN | No | false | | Cuadre. |
| postings_count | BIGINT | No | 0 | | Movimientos. |
| closed_at | TIMESTAMPTZ | Si | - | | - |
| closed_by | UUID | Si | - | | Job/usuario. |

## 8. Schema `credits`

### 8.1 `loan_products`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| code | VARCHAR(30) | No | - | UQ | - |
| name | VARCHAR(100) | No | - | | - |
| tcea | NUMERIC(18,8) | No | - | | Tasa anual. |
| tea | NUMERIC(18,8) | Si | - | | Tasa efectiva anual. |
| insurance_rate | NUMERIC(18,8) | No | 0 | | Desgravamen. |
| min_amount_minor | BIGINT | No | - | | - |
| max_amount_minor | BIGINT | No | - | | - |
| min_term_months | SMALLINT | No | - | | - |
| max_term_months | SMALLINT | No | - | | - |
| allowed_terms | SMALLINT[] | Si | - | | Plazos validos. |
| enabled | BOOLEAN | No | true | IDX | - |
| created_at | TIMESTAMPTZ | No | now() | | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |

### 8.2 `loan_applications`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| user_id | UUID | No | - | REF users, IDX | - |
| product_id | UUID | No | - | FK loan_products | - |
| requested_amount_minor | BIGINT | No | - | CK >0 | Monto pedido. |
| term_months | SMALLINT | No | - | | Plazo. |
| status | VARCHAR(20) | No | 'DRAFT' | IDX, CK | `loan_application_status`. |
| bureau_authorized | BOOLEAN | No | false | | Consentimiento. |
| bureau_authorized_at | TIMESTAMPTZ | Si | - | | - |
| score | INTEGER | Si | - | | Score interno/bureau. |
| dti_ratio | NUMERIC(6,4) | Si | - | | Deuda/ingreso. |
| total_income_minor | BIGINT | Si | - | | - |
| total_debt_minor | BIGINT | Si | - | | - |
| decision_reason | TEXT | Si | - | | Justificacion. |
| created_at | TIMESTAMPTZ | No | now() | IDX | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |
| decided_at | TIMESTAMPTZ | Si | - | | - |

### 8.3 `loans`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| application_id | UUID | No | - | FK loan_applications, UQ | - |
| account_id | UUID | No | - | REF accounts | Cuenta de desembolso. |
| principal_minor | BIGINT | No | - | CK >0 | Capital. |
| tcea | NUMERIC(18,8) | No | - | | Tasa. |
| term_months | SMALLINT | No | - | | Plazo. |
| insurance_rate | NUMERIC(18,8) | No | 0 | | - |
| outstanding_principal_minor | BIGINT | No | - | CK >=0 | Saldo de capital. |
| status | VARCHAR(20) | No | 'ACTIVE' | IDX, CK | `loan_status`. |
| disbursed_at | TIMESTAMPTZ | Si | - | | - |
| created_at | TIMESTAMPTZ | No | now() | | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |

### 8.4 `loan_schedules`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| loan_id | UUID | No | - | FK loans, UQ(con installment_no) | - |
| installment_no | SMALLINT | No | - | UQ(con loan_id) | Numero de cuota. |
| due_date | DATE | No | - | IDX | Vencimiento. |
| principal_minor | BIGINT | No | - | | Capital. |
| interest_minor | BIGINT | No | - | | Interes. |
| insurance_minor | BIGINT | No | 0 | | Seguro. |
| total_minor | BIGINT | No | - | | Cuota total. |
| status | VARCHAR(15) | No | 'PENDING' | IDX, CK | `installment_status`. |
| paid_at | TIMESTAMPTZ | Si | - | | - |

### 8.5 `loan_payments`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| loan_id | UUID | No | - | FK loans, IDX | - |
| schedule_id | UUID | Si | - | FK loan_schedules | Cuota pagada. |
| transaction_id | UUID | Si | - | REF transactions | Operacion en el motor. |
| amount_minor | BIGINT | No | - | CK >0 | Total pagado. |
| principal_applied_minor | BIGINT | No | 0 | | A capital. |
| interest_applied_minor | BIGINT | No | 0 | | A interes. |
| insurance_applied_minor | BIGINT | No | 0 | | A seguro. |
| paid_at | TIMESTAMPTZ | No | now() | | - |

### 8.6 `contracts`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| loan_id | UUID | No | - | FK loans, UQ | - |
| doc_ref | TEXT | No | - | | Ruta/almacen del documento. |
| doc_hash | VARCHAR(64) | No | - | | Hash del documento. |
| signature_token_hash | VARCHAR(128) | Si | - | | Token biometrico (hash). |
| status | VARCHAR(15) | No | 'GENERATED' | CK | `contract_status`. |
| signed_at | TIMESTAMPTZ | Si | - | | - |
| created_at | TIMESTAMPTZ | No | now() | | - |

## 9. Schema `wallet`

### 9.1 `wallets` y `wallet_links`

| Tabla | Columna | Tipo | Nulo | Default | Clave |
|---|---|---|---|---|---|
| wallets | id | UUID | No | gen_random_uuid() | PK |
| wallets | user_id | UUID | No | - | REF users, UQ |
| wallets | status | VARCHAR(15) | No | 'ACTIVE' | |
| wallets | created_at | TIMESTAMPTZ | No | now() | |
| wallet_links | id | UUID | No | gen_random_uuid() | PK |
| wallet_links | wallet_id | UUID | No | - | FK wallets |
| wallet_links | account_id | UUID | Si | - | REF accounts |
| wallet_links | token_ref | VARCHAR(120) | No | - | UQ |
| wallet_links | last4 | VARCHAR(4) | Si | - | |
| wallet_links | brand | VARCHAR(20) | Si | - | |
| wallet_links | status | VARCHAR(15) | No | 'ACTIVE' | |
| wallet_links | linked_at | TIMESTAMPTZ | No | now() | |

### 9.2 `merchant_accounts`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| user_id | UUID | No | - | REF users, UQ | - |
| business_name | VARCHAR(150) | No | - | | - |
| tax_id_hash | VARCHAR(128) | Si | - | | RUC hasheado. |
| status | VARCHAR(15) | No | 'ACTIVE' | | - |
| created_at | TIMESTAMPTZ | No | now() | | - |

### 9.3 `qr_charges`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| merchant_id | UUID | No | - | FK merchant_accounts, IDX | - |
| amount_minor | BIGINT | No | - | CK >0 | Monto. |
| currency | CHAR(3) | No | - | | Moneda. |
| concept | VARCHAR(120) | Si | - | | - |
| mode | VARCHAR(10) | No | 'DYNAMIC' | CK | `DYNAMIC`/`STATIC`. |
| payload_hash | VARCHAR(64) | No | - | | Hash del contenido. |
| signature | VARCHAR(256) | No | - | | Firma del QR. |
| status | VARCHAR(12) | No | 'ACTIVE' | IDX, CK | `qr_charge_status`. |
| expires_at | TIMESTAMPTZ | Si | - | IDX | - |
| created_at | TIMESTAMPTZ | No | now() | | - |
| paid_at | TIMESTAMPTZ | Si | - | | - |

### 9.4 `qr_payments`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| transaction_id | UUID | No | - | REF transactions, UQ | - |
| charge_id | UUID | No | - | FK qr_charges, IDX | - |
| payer_user_id | UUID | No | - | REF users, IDX | - |
| merchant_id | UUID | No | - | FK merchant_accounts | - |
| amount_minor | BIGINT | No | - | | - |
| currency | CHAR(3) | No | - | | - |
| status | VARCHAR(15) | No | 'PENDING' | | - |
| created_at | TIMESTAMPTZ | No | now() | | - |

### 9.5 `billers`, `service_payments`, `topups`, `service_subscriptions`

| Tabla | Columna | Tipo | Nulo | Default | Clave |
|---|---|---|---|---|---|
| billers | id | UUID | No | gen_random_uuid() | PK |
| billers | category | VARCHAR(30) | No | - | IDX |
| billers | name | VARCHAR(120) | No | - | |
| billers | required_fields | JSONB | No | '[]' | |
| billers | enabled | BOOLEAN | No | true | |
| service_payments | id | UUID | No | gen_random_uuid() | PK |
| service_payments | user_id | UUID | No | - | REF users |
| service_payments | biller_id | UUID | No | - | FK billers |
| service_payments | supply_ref | VARCHAR(80) | No | - | IDX |
| service_payments | amount_minor | BIGINT | No | - | CK >0 |
| service_payments | currency | CHAR(3) | No | - | |
| service_payments | transaction_id | UUID | Si | - | REF transactions |
| service_payments | receipt_data | JSONB | Si | - | |
| service_payments | status | VARCHAR(15) | No | 'PENDING' | |
| service_payments | created_at | TIMESTAMPTZ | No | now() | |
| topups | id | UUID | No | gen_random_uuid() | PK |
| topups | user_id | UUID | No | - | REF users |
| topups | operator | VARCHAR(40) | No | - | |
| topups | phone | VARCHAR(20) | No | - | |
| topups | amount_minor | BIGINT | No | - | CK >0 |
| topups | currency | CHAR(3) | No | 'PEN' | |
| topups | transaction_id | UUID | Si | - | REF transactions |
| topups | status | VARCHAR(15) | No | 'PENDING' | |
| topups | created_at | TIMESTAMPTZ | No | now() | |
| service_subscriptions | id | UUID | No | gen_random_uuid() | PK |
| service_subscriptions | user_id | UUID | No | - | REF users |
| service_subscriptions | biller_id | UUID | No | - | FK billers |
| service_subscriptions | supply_ref | VARCHAR(80) | No | - | |
| service_subscriptions | notify_days_before | SMALLINT | No | 3 | |
| service_subscriptions | status | VARCHAR(15) | No | 'ACTIVE' | |

## 10. Schema `fx`

### 10.1 `fx_rates`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| base | CHAR(3) | No | - | IDX | Moneda base. |
| quote | CHAR(3) | No | - | IDX | Moneda cotizada. |
| rate | NUMERIC(18,8) | No | - | CK >0 | Tipo de cambio. |
| source | VARCHAR(40) | No | - | | Proveedor/semilla. |
| fetched_at | TIMESTAMPTZ | No | now() | IDX | - |
| ttl_seconds | INTEGER | No | 60 | | Vigencia. |

### 10.2 `fx_quotes`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| user_id | UUID | No | - | REF users, IDX | - |
| base | CHAR(3) | No | - | | - |
| quote | CHAR(3) | No | - | | - |
| rate | NUMERIC(18,8) | No | - | | Congelada. |
| spread | NUMERIC(18,8) | No | 0 | | Aplicado. |
| source_amount_minor | BIGINT | No | - | | Monto origen. |
| target_amount_minor | BIGINT | No | - | | Monto destino. |
| expires_at | TIMESTAMPTZ | No | - | IDX | 30 s. |
| status | VARCHAR(12) | No | 'ACTIVE' | CK | `fx_quote_status`. |
| created_at | TIMESTAMPTZ | No | now() | | - |

### 10.3 `fx_operations`, `savings_pockets`, `pocket_movements`

| Tabla | Columna | Tipo | Nulo | Default | Clave |
|---|---|---|---|---|---|
| fx_operations | id | UUID | No | gen_random_uuid() | PK |
| fx_operations | quote_id | UUID | No | - | FK fx_quotes, UQ |
| fx_operations | transaction_id | UUID | No | - | REF transactions |
| fx_operations | status | VARCHAR(15) | No | 'EXECUTED' | |
| fx_operations | executed_at | TIMESTAMPTZ | No | now() | |
| savings_pockets | id | UUID | No | gen_random_uuid() | PK |
| savings_pockets | user_id | UUID | No | - | REF users, IDX |
| savings_pockets | account_id | UUID | Si | - | REF accounts |
| savings_pockets | name | VARCHAR(80) | No | - | |
| savings_pockets | currency | CHAR(3) | No | - | |
| savings_pockets | target_minor | BIGINT | Si | - | CK >=0 |
| savings_pockets | balance_minor | BIGINT | No | 0 | CK >=0 |
| savings_pockets | roundup_enabled | BOOLEAN | No | false | |
| savings_pockets | yield_rate | NUMERIC(18,8) | No | 0 | |
| savings_pockets | status | VARCHAR(15) | No | 'ACTIVE' | |
| savings_pockets | created_at | TIMESTAMPTZ | No | now() | |
| pocket_movements | id | UUID | No | gen_random_uuid() | PK |
| pocket_movements | pocket_id | UUID | No | - | FK savings_pockets |
| pocket_movements | journal_entry_id | UUID | Si | - | REF ledger |
| pocket_movements | movement_type | VARCHAR(20) | No | - | CK |
| pocket_movements | direction | VARCHAR(6) | No | - | DEBIT/CREDIT |
| pocket_movements | amount_minor | BIGINT | No | - | CK >0 |
| pocket_movements | currency | CHAR(3) | No | - | |
| pocket_movements | created_at | TIMESTAMPTZ | No | now() | |

## 11. Schema `risk`

### 11.1 `risk_rules`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| code | VARCHAR(40) | No | - | UQ | Identificador de regla. |
| name | VARCHAR(120) | No | - | | - |
| enabled | BOOLEAN | No | true | IDX | - |
| priority | SMALLINT | No | 100 | | Orden de evaluacion. |
| params_json | JSONB | No | '{}' | | Umbrales. |
| action | VARCHAR(15) | No | 'ALERT' | CK | `risk_action`. |
| created_at | TIMESTAMPTZ | No | now() | | - |
| updated_at | TIMESTAMPTZ | No | now() | | - |
| updated_by | UUID | Si | - | REF users | - |

### 11.2 `risk_profiles`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| user_id | UUID | No | - | PK, REF users | - |
| avg_amount_minor | BIGINT | No | 0 | | Promedio. |
| usual_countries | TEXT[] | Si | - | | Paises habituales. |
| usual_hours | SMALLINT[] | Si | - | | Horas habituales. |
| last_device_id | VARCHAR(128) | Si | - | | - |
| score | SMALLINT | No | 0 | | Puntaje. |
| updated_at | TIMESTAMPTZ | No | now() | | - |

### 11.3 `alerts`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| rule_code | VARCHAR(40) | Si | - | | Regla disparada. |
| type | VARCHAR(30) | No | - | IDX | Tipo de alerta. |
| severity | VARCHAR(10) | No | - | IDX, CK | `alert_severity`. |
| user_id | UUID | Si | - | REF users, IDX | - |
| transaction_id | UUID | Si | - | REF transactions | - |
| detail | JSONB | Si | - | | Evidencia. |
| status | VARCHAR(15) | No | 'OPEN' | IDX, CK | `alert_status`. |
| assigned_to | UUID | Si | - | REF users | - |
| created_at | TIMESTAMPTZ | No | now() | IDX | - |
| resolved_at | TIMESTAMPTZ | Si | - | | - |

### 11.4 `screening_results`, `ros_reports`, `blocked_entities`

| Tabla | Columna | Tipo | Nulo | Default | Clave |
|---|---|---|---|---|---|
| screening_results | id | UUID | No | gen_random_uuid() | PK |
| screening_results | subject_type | VARCHAR(15) | No | - | CK |
| screening_results | subject_ref | UUID | No | - | IDX |
| screening_results | list_code | VARCHAR(30) | No | - | OFAC/PEP/... |
| screening_results | matched | BOOLEAN | No | - | |
| screening_results | match_score | NUMERIC(6,4) | Si | - | |
| screening_results | matched_fields | JSONB | Si | - | |
| screening_results | reviewed_by | UUID | Si | - | REF users |
| screening_results | created_at | TIMESTAMPTZ | No | now() | |
| ros_reports | id | UUID | No | gen_random_uuid() | PK |
| ros_reports | alert_id | UUID | Si | - | FK alerts |
| ros_reports | subject_ref | UUID | No | - | |
| ros_reports | narrative | TEXT | No | - | |
| ros_reports | status | VARCHAR(15) | No | 'DRAFT' | CK |
| ros_reports | created_by | UUID | No | - | REF users |
| ros_reports | created_at | TIMESTAMPTZ | No | now() | |
| ros_reports | filed_at | TIMESTAMPTZ | Si | - | |
| blocked_entities | id | UUID | No | gen_random_uuid() | PK |
| blocked_entities | entity_type | VARCHAR(20) | No | - | CK |
| blocked_entities | entity_ref | VARCHAR(120) | No | - | IDX |
| blocked_entities | reason | TEXT | No | - | |
| blocked_entities | blocked_until | TIMESTAMPTZ | Si | - | |
| blocked_entities | created_by | UUID | Si | - | REF users |
| blocked_entities | created_at | TIMESTAMPTZ | No | now() | |

## 12. Schema `reconciliation`

### 12.1 `clearing_files`, `clearing_items`

| Tabla | Columna | Tipo | Nulo | Default | Clave |
|---|---|---|---|---|---|
| clearing_files | id | UUID | No | gen_random_uuid() | PK |
| clearing_files | source | VARCHAR(40) | No | - | IDX |
| clearing_files | file_ref | VARCHAR(80) | No | - | UQ |
| clearing_files | storage_ref | TEXT | Si | - | |
| clearing_files | checksum | VARCHAR(64) | Si | - | |
| clearing_files | item_count | INTEGER | No | 0 | |
| clearing_files | status | VARCHAR(15) | No | 'RECEIVED' | CK |
| clearing_files | received_at | TIMESTAMPTZ | No | now() | |
| clearing_items | id | UUID | No | gen_random_uuid() | PK |
| clearing_items | file_id | UUID | No | - | FK clearing_files |
| clearing_items | external_ref | VARCHAR(80) | No | - | IDX |
| clearing_items | amount_minor | BIGINT | No | - | |
| clearing_items | currency | CHAR(3) | No | - | |
| clearing_items | value_date | DATE | No | - | |
| clearing_items | direction | VARCHAR(10) | No | - | INBOUND/OUTBOUND |
| clearing_items | status | VARCHAR(15) | No | 'PENDING' | CK |
| clearing_items | raw | JSONB | Si | - | |

### 12.2 `reconciliation_runs`, `reconciliation_exceptions`, `claims`

| Tabla | Columna | Tipo | Nulo | Default | Clave |
|---|---|---|---|---|---|
| reconciliation_runs | id | UUID | No | gen_random_uuid() | PK |
| reconciliation_runs | run_date | DATE | No | - | IDX |
| reconciliation_runs | currency | CHAR(3) | No | - | |
| reconciliation_runs | status | VARCHAR(15) | No | 'RUNNING' | |
| reconciliation_runs | matched_count | INTEGER | No | 0 | |
| reconciliation_runs | unmatched_count | INTEGER | No | 0 | |
| reconciliation_runs | total_minor | BIGINT | No | 0 | |
| reconciliation_runs | executed_by | UUID | Si | - | REF users |
| reconciliation_runs | created_at | TIMESTAMPTZ | No | now() | |
| reconciliation_runs | finished_at | TIMESTAMPTZ | Si | - | |
| reconciliation_exceptions | id | UUID | No | gen_random_uuid() | PK |
| reconciliation_exceptions | run_id | UUID | No | - | FK runs |
| reconciliation_exceptions | transaction_id | UUID | Si | - | REF transactions |
| reconciliation_exceptions | clearing_item_id | UUID | Si | - | FK clearing_items |
| reconciliation_exceptions | type | VARCHAR(30) | No | - | CK |
| reconciliation_exceptions | difference_minor | BIGINT | No | 0 | |
| reconciliation_exceptions | status | VARCHAR(15) | No | 'OPEN' | |
| reconciliation_exceptions | created_at | TIMESTAMPTZ | No | now() | |
| claims | id | UUID | No | gen_random_uuid() | PK |
| claims | exception_id | UUID | No | - | FK exceptions |
| claims | description | TEXT | Si | - | |
| claims | status | VARCHAR(15) | No | 'OPEN' | CK |
| claims | resolution | TEXT | Si | - | |
| claims | resolved_by | UUID | Si | - | REF users |
| claims | created_at | TIMESTAMPTZ | No | now() | |
| claims | resolved_at | TIMESTAMPTZ | Si | - | |

## 13. Schema `notifications`

| Tabla | Columna | Tipo | Nulo | Default | Clave |
|---|---|---|---|---|---|
| notification_templates | id | UUID | No | gen_random_uuid() | PK |
| notification_templates | code | VARCHAR(50) | No | - | UQ |
| notification_templates | channel | VARCHAR(10) | No | - | CK |
| notification_templates | subject | VARCHAR(150) | Si | - | |
| notification_templates | body_template | TEXT | No | - | |
| notification_templates | enabled | BOOLEAN | No | true | |
| notifications | id | UUID | No | gen_random_uuid() | PK |
| notifications | user_id | UUID | Si | - | REF users, IDX |
| notifications | channel | VARCHAR(10) | No | - | CK |
| notifications | template_code | VARCHAR(50) | Si | - | |
| notifications | payload_json | JSONB | Si | - | |
| notifications | status | VARCHAR(12) | No | 'QUEUED' | IDX, CK |
| notifications | provider_ref | VARCHAR(120) | Si | - | |
| notifications | error | TEXT | Si | - | |
| notifications | created_at | TIMESTAMPTZ | No | now() | IDX |
| notifications | sent_at | TIMESTAMPTZ | Si | - | |
| notifications | read_at | TIMESTAMPTZ | Si | - | |
| user_channel_preferences | user_id | UUID | No | - | PK, REF users |
| user_channel_preferences | push | BOOLEAN | No | true | |
| user_channel_preferences | email | BOOLEAN | No | true | |
| user_channel_preferences | sms | BOOLEAN | No | false | |
| user_channel_preferences | updated_at | TIMESTAMPTZ | No | now() | |

## 14. Schema `audit`

### 14.1 `audit_log` (append-only)

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| seq | BIGSERIAL | No | - | UQ, IDX | Secuencia. |
| actor_type | VARCHAR(20) | No | - | | `USER`/`SYSTEM`/`SERVICE`. |
| actor_id | UUID | Si | - | IDX | - |
| action | VARCHAR(60) | No | - | IDX | Accion. |
| entity_type | VARCHAR(60) | No | - | IDX | Entidad. |
| entity_id | UUID | Si | - | IDX | - |
| before_json | JSONB | Si | - | | Estado previo. |
| after_json | JSONB | Si | - | | Estado nuevo. |
| ip | INET | Si | - | | - |
| device_id | VARCHAR(128) | Si | - | | - |
| request_id | VARCHAR(60) | Si | - | IDX | Correlacion. |
| created_at | TIMESTAMPTZ | No | now() | IDX | - |
| prev_hash | VARCHAR(64) | Si | - | | Hash anterior. |
| hash | VARCHAR(64) | No | - | | Hash del registro. |

### 14.2 `audit_verifications`

| Columna | Tipo | Nulo | Default | Clave | Descripcion |
|---|---|---|---|---|---|
| id | UUID | No | gen_random_uuid() | PK | - |
| checked_at | TIMESTAMPTZ | No | now() | | - |
| from_seq | BIGINT | No | - | | Rango inicio. |
| to_seq | BIGINT | No | - | | Rango fin. |
| valid | BOOLEAN | No | - | | Cadena integra. |
| details | JSONB | Si | - | | - |

## 15. Indices recomendados (resumen)

| Tabla | Indice |
|---|---|
| users | `(doc_number_hash)` UQ, `(email)` UQ, `(phone)`, `(status)` |
| device_bindings | `(user_id, device_id)` UQ |
| otp_codes | `(user_id, purpose, status)`, `(expires_at)` |
| sessions | `(refresh_token_hash)` UQ, `(user_id)`, `(expires_at)` |
| accounts | `(user_id)`, `(account_number)` UQ |
| account_balances | `(account_id)` PK |
| beneficiaries | `(owner_user_id, alias)` |
| movements_view | `(account_id, created_at DESC)` |
| transactions | `(idempotency_key)`, `(status, created_at)`, `(source_account_id)`, `(external_ref)` |
| transaction_status_history | `(transaction_id, created_at)` |
| holds | `(status, expires_at)`, `(account_id)` |
| idempotency_keys | `(key, user_id)` UQ, `(expires_at)` |
| journal_entries | `(transaction_id)`, `(value_date)`, `(created_at)` |
| postings | `(journal_entry_id)`, `(ledger_account_id, created_at)` |
| loan_schedules | `(loan_id, due_date, status)` |
| qr_charges | `(merchant_id, status)`, `(expires_at)` |
| fx_rates | `(base, quote, fetched_at DESC)` |
| alerts | `(status, severity, created_at DESC)`, `(user_id)` |
| clearing_items | `(file_id)`, `(external_ref)` |
| notifications | `(user_id, created_at DESC)`, `(status)` |
| audit_log | `(seq)` UQ, `(entity_type, entity_id)`, `(created_at)`, `(action)` |
| outbox | `(status, available_at)` |

## 16. Reglas de integridad a nivel BD

1. `account_balances.available_minor >= 0` y `held_minor >= 0`.
2. `transactions.amount_minor > 0` y `fee_minor >= 0`.
3. `postings.amount_minor > 0`; por `journal_entry_id`, `sum(DEBIT)=sum(CREDIT)` por moneda
   (se valida en aplicacion y en job de control).
4. `loan_schedules`: `principal_minor + interest_minor + insurance_minor = total_minor`.
5. `fx_quotes`: `expires_at > created_at`.
6. `savings_pockets.balance_minor >= 0`.
7. `audit_log` y `postings`: sin `UPDATE`/`DELETE` (permisos de BD y triggers de bloqueo).
8. `ledger_accounts.code` unico.
9. Idempotencia: `idempotency_keys(key, user_id)` unico.
