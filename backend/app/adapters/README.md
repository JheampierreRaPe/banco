# Adaptadores externos

Clientes hacia servicios externos o simulados. Cada adaptador expone una interfaz comun y una
implementacion `mock` activable por configuracion.

| Adaptador | Simula | Nota |
|---|---|---|
| `KycProvider` | RENIEC + OCR + liveness | **Real**: microservicio FastAPI existente; solo onboarding (HU01). |
| `DocumentLookupProvider` | Titular por DNI/RUC (apiinti) | **Real**: `GET /dni/{n}`, `/ruc/{n}` con `Bearer APIINTI_API_KEY` server-side (E1-T35); mock por defecto. |
| `HolderLookup` | Titular/CCI de cuenta destino (simulador interbancario D01) | Interfaz + mock determinista + HTTP con timeout/reintentos/circuit breaker (E2-T08); se consume por la fachada `transactions.service.validate_holder`, **sin endpoint HTTP** (D-07). |
| `CreditBureau` | Central de riesgo | Interfaz + `MockCreditBureau` determinista por perfil (`prime`/`standard`/`subprime`, fixtures por sujeto, modos `success`/`down`/`timeout`) + `HttpCreditBureau` (`POST /score -> {score, deudas, morosidad}`, timeout/reintentos con backoff/circuit breaker) con fabrica `create_credit_bureau_provider` (`CREDIT_BUREAU_PROVIDER=mock\|http`, default `mock`; `CREDIT_BUREAU_BASE_URL`, `CREDIT_BUREAU_API_KEY` server-side, `CREDIT_BUREAU_TIMEOUT_SECONDS`, `CREDIT_BUREAU_MAX_RETRIES`); errores tipados `BUREAU_UNAVAILABLE`/`BUREAU_TIMEOUT`; sin PII ni secretos en logs (E3-T06, HU10). |
| `FxRateProvider` | API de tipo de cambio | Fallback a tasa semilla. |
| `InterbankGateway` | Red interbancaria / equipo par | **Simulador** con contrato propio (D01). |
| `WalletInterop` | Billetera interoperable / equipo par (HU13 CA-03) | Interfaz `WalletInteropGateway` (`encode/decode` local del sobre `BO-QR1` + `validate_counterpart`) + `MockWalletInteropGateway` determinista (modos `success`/`down`/`timeout`, `counterparts`/`rejected` configurables, sin red) + `HttpWalletInteropGateway` (`POST {base_url}/interop/counterparts/validate`, timeout/reintentos/circuito) con fabrica `create_wallet_interop_provider` (`WALLET_INTEROP_PROVIDER=mock\|http`, default `mock`; `WALLET_INTEROP_BASE_URL`, `WALLET_INTEROP_API_KEY` server-side, `WALLET_INTEROP_TIMEOUT_SECONDS`, `WALLET_INTEROP_MAX_RETRIES`); errores tipados `INTEROP_UNAVAILABLE`/`INTEROP_TIMEOUT`; `sig` opaco (la firma local la fija `E4-T06`); sin PII ni secretos en logs; no mueve dinero ni persiste (E4-T03). |
| `ServiceProvider` | Empresas de servicios / suministro (HU16 CA-01) | Interfaz `ServiceProviderGateway` (`lookup_biller` por nombre/suministro + `validate_supply`) + `MockServiceProviderGateway` determinista (modos `success`/`down`/`timeout`, catalogo demo espejo de la siembra `0042`, deuda simulada derivada del hash, sin red) + `HttpServiceProviderGateway` (`POST {base_url}/services/supply/validate`, timeout/reintentos con backoff/circuit breaker) con fabrica `create_service_provider` (`SERVICE_PROVIDER=mock\|http`, default `mock`, valor no reconocido -> `mock`; `SERVICE_PROVIDER_BASE_URL`, `SERVICE_PROVIDER_API_KEY` server-side, `SERVICE_PROVIDER_TIMEOUT_SECONDS`, `SERVICE_PROVIDER_MAX_RETRIES`); errores tipados `SERVICE_PROVIDER_UNAVAILABLE` (`503`)/`SERVICE_PROVIDER_TIMEOUT` (`502`); sin PII ni secretos en logs (solo hash del suministro); no mueve dinero ni persiste (E4-T16; el pago es `E4-T17`). |
| `ClearingSource` | Archivos de compensacion | Lote CSV/JSON simulado. |
| `SanctionsLists` | OFAC / World-Check / PEP | Cotejo difuso simulado. |
| `Notifications` | Push / correo / SMS | Multi-canal desacoplado por canal (E6-T13, HU22): `sender_for_channel` resuelve `push` por `PUSH_PROVIDER` (`PushNotificationSender` mock determinista con guiones `ok`/`down`/`timeout`, sin red), `email` por `EMAIL_PROVIDER` (`mock`\|`gmail`), `sms` por `SMS_PROVIDER` (`mock`\|`twilio`); `mock` por defecto y valor desconocido -> `ValueError` fail-fast; el fallo de un proveedor queda aislado por canal (`NotificationProviderError`, sin PII en logs). |

Reglas: timeouts, reintentos con backoff y circuit breaker en todos. Nunca exponer API keys al
cliente movil. Ver `docs/02-arquitectura.md` seccion 9.
