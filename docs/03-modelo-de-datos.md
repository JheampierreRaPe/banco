# 03 - Modelo de datos

> Version ejecutable: `03b-diccionario-de-datos.md` (columna por columna) y `03c-modelo-er.md`
> (relaciones y agregados). Este documento es la vision general.

## 1. Reglas transversales

- **Dinero**: `BIGINT` en centimos (`*_minor`) + `currency` ISO-4217. Nunca `FLOAT`.
- **Tasas/porcentajes**: `NUMERIC(12,6)`.
- **IDs**: `UUID` v4/v7. Nunca autincrementales expuestos.
- **Tiempos**: `TIMESTAMPTZ` UTC; `created_at`, `updated_at`.
- **Borrado**: prohibido en tablas financieras y de auditoria; se usa `status` o reverso.
- **Multimoneda**: moneda por cuenta/bolsillo; los asientos no cruzan monedas (cada asiento
  cuadra en una sola moneda; el cambio de divisas genera dos asientos ligados).
- **Un schema por modulo**, con el mismo nombre del modulo.
- **Parametros** (umbrales, tasas, limites) en tabla `config.parameters`, no en el codigo.

## 2. Schema `identity`

| Tabla | Campos clave | Notas |
|---|---|---|
| `users` | id, doc_type, doc_number_hash, doc_number_masked, first_name, last_name, email, phone, status, risk_profile_id | `doc_number_hash` unico (no se guarda el numero en claro salvo permiso). |
| `credentials` | user_id, password_hash, pin_hash, biometric_enabled, failed_attempts, locked_until | Bloqueo temporal tras 5 intentos. |
| `kyc_verifications` | id, user_id, provider, overall_result, document_json, liveness_json, face_match_json, challenge_token, created_at | Guarda el **resultado**, no los frames. |
| `otp_codes` | id, user_id, purpose, code_hash, expires_at, attempts, status | Un solo uso; expira a 10 min. |
| `sessions` | id, user_id, device, ip, refresh_token_hash, expires_at, revoked_at | Permite revocar sesiones. |
| `roles`, `user_roles` | code/name, user_id+role_id | RBAC. |
| `access_recovery` | id, user_id, method, biometric_result, new_credential_set, notified_at | HU04. |

## 3. Schema `accounts`

| Tabla | Campos clave | Notas |
|---|---|---|
| `accounts` | id, user_id, account_number, type, currency, status, ledger_account_id | Numero enmascarado en respuestas. |
| `account_balances` | account_id, currency, available_minor, held_minor, version | **Proyeccion** del ledger; se actualiza en la misma transaccion. |
| `beneficiaries` | id, owner_user_id, alias, photo_url, bank_code, account_or_cci, holder_name, preapproved_limit_minor, status | HU07. |
| `movements_view` | account_id, journal_entry_id, direction, amount_minor, currency, description, value_date | Vista de lectura (CQRS ligero). |

Invariante: `available_minor + held_minor = saldo contable de la cuenta en el ledger`.

## 4. Schema `transactions`

| Tabla | Campos clave | Notas |
|---|---|---|
| `transactions` | id, type, status, idempotency_key, initiator_user_id, source_account_id, target_account_id, external_ref, amount_minor, currency, fee_minor, risk_level, failure_reason, version | Nucleo del motor. |
| `transaction_status_history` | id, transaction_id, from_status, to_status, reason, actor, created_at | Trazabilidad de estados. |
| `holds` | id, transaction_id, account_id, amount_minor, currency, status, expires_at | Reserva de fondos. |

Tipos: `own_transfer`, `third_party_transfer`, `interbank_transfer`, `qr_payment`,
`service_payment`, `topup`, `loan_disbursement`, `loan_installment`, `fx_exchange`,
`pocket_transfer`, `salary_distribution`, `reversal`.

Estados: ver doc `04`. Nunca se borra una transaccion; un fallo se compensa con reverso.

## 5. Schema `ledger`

| Tabla | Campos clave | Notas |
|---|---|---|
| `ledger_accounts` | id, code, name, type (`asset/liability/equity/income/expense`), currency, owner_ref, is_system | Catalogo contable. |
| `journal_entries` | id, transaction_id, description, value_date, status (`posted/reversed`), prev_hash, hash | Cabecera del asiento. |
| `postings` | id, journal_entry_id, ledger_account_id, direction (`debit/credit`), amount_minor, currency, account_ref | Lineas del asiento. **Append-only**. |
| `ledger_balances` | ledger_account_id, currency, balance_minor, version | Proyeccion. |
| `daily_closings` | closing_date, total_debits_minor, total_credits_minor, balanced, closed_at | HU18 cierre diario. |

Invariantes:
1. Por cada `journal_entry`, `sum(debits) = sum(credits)` en su moneda.
2. Un asiento se inserta completo o no se inserta (atomicidad).
3. `postings` no admite `UPDATE` ni `DELETE`; una correccion es un asiento nuevo.
4. `hash` encadena cada asiento con el anterior (no repudio basico).

### Catalogo contable semilla (simplificado)

| Cuenta | Tipo | Uso |
|---|---|---|
| `1000 Caja/Banco` | Asset | Dinero del banco simulado. |
| `2000 Cuenta de cliente` | Liability | Saldo a favor del cliente. |
| `2100 Fondos retenidos` | Liability | Holds. |
| `2200 Cuentas de transito` | Liability | Operaciones interbancarias en curso. |
| `3000 Patrimonio` | Equity | Aportes. |
| `4000 Ingresos por comisiones` | Income | Fees. |
| `4100 Ingresos por intereses` | Income | Credito. |
| `5000 Cartera de prestamos` | Asset | Capital colocado. |
| `6000 Gastos` | Expense | Costos. |

## 6. Schema `credits`

| Tabla | Campos clave | Notas |
|---|---|---|
| `loan_products` | id, name, tcea, tea, insurance_rate, min_amount_minor, max_amount_minor, allowed_terms, enabled | Configurable. |
| `loan_applications` | id, user_id, product_id, requested_amount_minor, term_months, status, score, dti_ratio, decision_reason, authorized_bureau, created_at | HU10. |
| `loans` | id, application_id, account_id, principal_minor, tcea, term_months, status, disbursed_at | HU12. |
| `loan_schedules` | id, loan_id, installment_no, due_date, principal_minor, interest_minor, insurance_minor, total_minor, status | Cronograma. |
| `loan_payments` | id, loan_id, schedule_id, transaction_id, paid_at | Liga al motor. |
| `contracts` | id, loan_id, doc_ref, signature_token_hash, signed_at, hash | HU11. |

## 7. Schema `wallet`

| Tabla | Campos clave | Notas |
|---|---|---|
| `wallets` | id, user_id, status | HU13. |
| `wallet_links` | id, wallet_id, account_id, token_ref, status | No se guarda PAN completo. |
| `merchant_accounts` | id, user_id, business_name, status | Comercio. |
| `qr_charges` | id, merchant_id, amount_minor, concept, status, expires_at, signature | HU14. |
| `qr_payments` | id, transaction_id, charge_id, payer_user_id, merchant_id, status | HU15. |
| `billers` | id, category, name, required_fields, enabled | Catalogo servicios. |
| `service_payments` | id, user_id, biller_id, supply_ref, amount_minor, transaction_id, receipt_data, status | HU16. |
| `topups` | id, user_id, operator, phone, amount_minor, transaction_id, status | HU16. |

## 8. Schema `fx`

| Tabla | Campos clave | Notas |
|---|---|---|
| `fx_rates` | id, base, quote, rate, source, fetched_at, ttl | Origen real o semilla. |
| `fx_quotes` | id, user_id, base, quote, rate, amount_minor, spread, expires_at, status | Congelada 30 s. |
| `fx_operations` | id, quote_id, transaction_id, status | HU25. |
| `savings_pockets` | id, user_id, name, currency, target_minor, balance_minor, roundup_enabled | HU24. |
| `pocket_movements` | id, pocket_id, journal_entry_id, direction, amount_minor, created_at | Traspasos. |

## 9. Schema `risk`

| Tabla | Campos clave | Notas |
|---|---|---|
| `risk_rules` | id, code, name, enabled, priority, params_json, action | Antifraude configurable. |
| `risk_profiles` | user_id, avg_amount_minor, usual_geo, usual_hours, score | Perfil de comportamiento. |
| `alerts` | id, type, severity, user_id, transaction_id, status, assigned_to, created_at | HU20. |
| `screening_results` | id, subject_type, subject_ref, list_code, matched, score, reviewed_by | HU21. |
| `ros_reports` | id, alert_id, status, created_by, created_at | HU21. |
| `blocked_entities` | id, entity_type, entity_ref, reason, blocked_until | Bloqueos preventivos. |

## 10. Schema `reconciliation`

| Tabla | Campos clave | Notas |
|---|---|---|
| `clearing_files` | id, source, file_ref, received_at, status | HU19. |
| `clearing_items` | id, file_id, external_ref, amount_minor, currency, value_date, status | Una linea por operacion del tercero. |
| `reconciliation_runs` | id, run_date, status, matched_count, unmatched_count | Corrida diaria. |
| `reconciliation_exceptions` | id, run_id, transaction_id, clearing_item_id, difference_minor, type, status | Diferencias. |
| `claims` | id, exception_id, status, resolution, resolved_by | Flujo de reclamos. |

## 11. Schemas `notifications`, `audit`, `config`

| Tabla | Campos clave | Notas |
|---|---|---|
| `notifications` | id, user_id, channel (`push/email/sms`), template_code, payload_json, status, sent_at, read_at, provider_ref | HU22. |
| `notification_templates` | id, code, channel, subject, body | Plantillas. |
| `audit_log` | id, seq, actor, action, entity_type, entity_id, before_json, after_json, ip, device, created_at, prev_hash, hash | Append-only + hash encadenado. HU23. |
| `parameters` | key, value_json, module, description, updated_by | Toda regla configurable. |
| `permissions` / `role_permissions` | code, role_code | Matriz de accesos. |

## 12. Tablas transversales de fiabilidad

| Tabla | Proposito |
|---|---|
| `outbox` | Eventos a publicar; se inserta con la transaccion de negocio. |
| `inbox` / `processed_events` | Eventos ya procesados por consumidor (idempotencia). |
| `idempotency_keys` | key, user_id, endpoint, request_hash, response_snapshot, created_at (TTL). |

## 13. Integridad y controles

1. **Cuadre diario**: `sum(debits) = sum(credits)` por dia y moneda; si falla, se alerta.
2. **Saldo no negativo**: el motor rechaza si `available_minor < amount`.
3. **Holds**: `held_minor` nunca negativo; los holds vencidos se liberan por job.
4. **Consistencia**: `accounts.account_balances` debe coincidir con la suma de `postings`;
   un job de verificacion reporta desviaciones.
5. **Idempotencia**: `idempotency_key` unico; repetir devuelve la respuesta guardada.
6. **Concurrencia**: bloqueo de filas de saldo en orden estable de `account_id`.
7. **Append-only**: `postings`, `audit_log` y `journal_entries` no se actualizan ni borran.
