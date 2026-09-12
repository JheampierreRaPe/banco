# 03c - Modelo entidad-relacion y relaciones entre modulos

> Companero de `03b-diccionario-de-datos.md`. Muestra como se relacionan las tablas, que
> entidades forman un "agregado" (se escriben juntas de forma transaccional) y que referencias
> cruzan modulos (logicas, sin FK de base de datos).

## 1. Convenciones del diagrama

- `──<` relacion uno-a-muchos. `──1` uno-a-uno. `><` muchos-a-muchos.
- `(REF)` referencia logica entre modulos: se guarda el UUID, **no** hay FK fisica.
- Los agregados se marcan como `[AGREGADO: nombre]`.

## 2. Modulo `identity`

```
[AGREGADO: User]
users ──1 credentials
      ──< kyc_verifications      (1 registro exitoso en adelante; varios intentos)
      ──< otp_codes              (activacion, recuperacion, pago)
      ──< sessions
      ──< device_bindings        (dispositivos confiables para login biometrico)
      ──< access_recovery
      >< roles  (via user_roles)

roles ──< user_roles ──> users
```

Notas:
- `doc_number_hash` es unico: evita cuentas duplicadas por documento.
- `device_bindings` habilita el login con biometria del dispositivo (nonce firmado).
- `kyc_verifications` **nunca** guarda imagenes ni frames.

## 3. Modulo `accounts`

```
[AGREGADO: Account]
users (REF) ──< accounts ──1 account_balances
                        ──< daily_balance_snapshots
                        ──< movements_view          (proyeccion de lectura)

[AGREGADO: Beneficiary]
users (REF) ──< beneficiaries
```

Notas:
- `accounts.ledger_account_id` y `ledger_hold_account_id` apuntan a `ledger.ledger_accounts`
  (REF). Cada cuenta de cliente tiene dos cuentas contables: disponible (`2000`) y retenido
  (`2100`).
- `movements_view` se alimenta de eventos del ledger; no es fuente de verdad.

## 4. Modulo `transactions`

```
[AGREGADO: Transaction]
users (REF)      ──< transactions
accounts (REF)   ──< transactions  (source_account_id / target_account_id)
transactions ──< transaction_status_history
             ──< holds
idempotency_keys ──1 transactions  (transaction_id)
```

Estados de la maquina (resumen): `INITIATED -> VALIDATED -> [PENDING_AUTHORIZATION] ->
AUTHORIZED -> FUNDS_HELD -> POSTED -> SETTLED -> CONCILIATED`, con ramas `REJECTED`, `FAILED`,
`REVERSED`. Ver `04`.

Notas:
- `holds` es la representacion operativa de la retencion; el efecto contable se refleja en los
  `postings` entre `2000-<cuenta>` y `2100-<cuenta>`.
- Un `FAILED` siempre libera holds.

## 5. Modulo `ledger`

```
[AGREGADO: JournalEntry]
transactions (REF) ──< journal_entries ──< postings ──> ledger_accounts
                             │  (reverses_entry_id: self-reference)
                             └─1? (cada asiento puede revertir a otro)

ledger_accounts ──1 ledger_balances
daily_closings  (sin relacion; control agregado por fecha+moneda)
```

Reglas del agregado:
- `journal_entries` + `postings` se insertan **juntos** en una transaccion.
- `postings` es append-only; la correccion es otro `journal_entry` con `reverses_entry_id`.
- `ledger_balances` es proyeccion; se actualiza en la misma transaccion.

## 6. Modulo `credits`

```
[AGREGADO: LoanApplication]
users (REF) ──< loan_applications ──> loan_products

[AGREGADO: Loan]
loan_applications ──1 loans ──< loan_schedules
                            ──< loan_payments ──> (REF) transactions
                            ──1 contracts
accounts (REF) <── loans (account_id de desembolso)
```

Notas:
- `loan_schedules` se genera al desembolsar y es la base del cobro y de los recordatorios.
- `loan_payments.transaction_id` enlaza con el motor transaccional (pago de cuota).

## 7. Modulo `wallet`

```
[AGREGADO: Wallet]
users (REF) ──1 wallets ──< wallet_links ──> accounts (REF)

[AGREGADO: Merchant]
users (REF) ──1 merchant_accounts ──< qr_charges ──< qr_payments
                                          │              └──> (REF) transactions
                                          └── status / expires_at

billers ──< service_payments ──> (REF) transactions
       ──< service_subscriptions
topups ──> (REF) transactions
```

## 8. Modulo `fx`

```
[AGREGADO: FxQuote]
users (REF) ──< fx_quotes ──1 fx_operations ──> (REF) transactions
        └── base/quote/rate/expires_at

[AGREGADO: SavingsPocket]
users (REF) ──< savings_pockets ──< pocket_movements ──> (REF) ledger
```

## 9. Modulo `risk`

```
[AGREGADO: Alert]
risk_rules ──< alerts (por rule_code) ──? ros_reports
users (REF) ──1 risk_profiles
users/transactions (REF) <── alerts / screening_results
blocked_entities  (por entidad)
```

## 10. Modulo `reconciliation`

```
[AGREGADO: ReconciliationRun]
clearing_files ──< clearing_items
reconciliation_runs ──< reconciliation_exceptions ──< claims
transactions (REF) <── reconciliation_exceptions
```

## 11. Modulo `notifications`

```
users (REF) ──< notifications ──> notification_templates (por code)
            ──1 user_channel_preferences
```

## 12. Modulo `audit`

```
[AGREGADO: AuditEntry]  (append-only, hash encadenado por seq)
audit_log  ── referencia cualquier entidad por (entity_type, entity_id)
audit_verifications  ── control de integridad de la cadena
```

## 13. Referencias entre modulos (REF, sin FK fisica)

| Origen | Campo | Destino | Tipo |
|---|---|---|---|
| accounts | user_id | identity.users.id | N:1 |
| accounts | ledger_account_id / ledger_hold_account_id | ledger.ledger_accounts.id | N:1 |
| beneficiaries | owner_user_id | identity.users.id | N:1 |
| transactions | initiator_user_id | identity.users.id | N:1 |
| transactions | source_account_id / target_account_id | accounts.accounts.id | N:1 |
| holds | account_id | accounts.accounts.id | N:1 |
| journal_entries | transaction_id | transactions.transactions.id | N:1 |
| loan_applications | user_id | identity.users.id | N:1 |
| loans | account_id | accounts.accounts.id | N:1 |
| loan_payments | transaction_id | transactions.transactions.id | N:1 |
| wallet_links | account_id | accounts.accounts.id | N:1 |
| qr_payments | transaction_id | transactions.transactions.id | N:1 |
| service_payments / topups | transaction_id | transactions.transactions.id | N:1 |
| fx_operations | transaction_id | transactions.transactions.id | N:1 |
| alerts | transaction_id | transactions.transactions.id | N:1 |
| alerts / screening_results | user_id | identity.users.id | N:1 |
| reconciliation_exceptions | transaction_id | transactions.transactions.id | N:1 |
| notifications | user_id | identity.users.id | N:1 |
| audit_log | actor_id, entity_id | (cualquier entidad) | logica |

## 14. Fuente de verdad y proyecciones

| Dato | Fuente de verdad | Proyeccion |
|---|---|---|
| Saldo contable | `ledger.postings` | `ledger.ledger_balances` |
| Saldo disponible/retenido | `transactions` + `postings` | `accounts.account_balances` |
| Movimientos por cuenta | `ledger.postings` | `accounts.movements_view` |
| Estado de operacion | `transactions.transactions` | `transaction_status_history` (historial) |
| Cuotas | `credits.loan_schedules` | - |
| Cotizacion | `fx.fx_rates` | `fx.fx_quotes` (congelada) |

Regla: si una proyeccion difiere del origen, gana el origen y se emite alerta de integridad.

## 15. Invariantes del modelo

1. `available_minor + held_minor` coincide con la suma de `postings` de la cuenta.
2. Cada `journal_entry` cuadra en su moneda.
3. No hay FK entre modulos; la integridad cruzada se mantiene por eventos y jobs de control.
4. Ningun asiento, postings o registro de auditoria se modifica o elimina.
5. Toda operacion de dinero referencia un `transaction_id` y al menos un `journal_entry`.
6. `loan_schedules` suma el total financiado; los pagos no exceden el total.
