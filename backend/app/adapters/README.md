# Adaptadores externos

Clientes hacia servicios externos o simulados. Cada adaptador expone una interfaz comun y una
implementacion `mock` activable por configuracion.

| Adaptador | Simula | Nota |
|---|---|---|
| `KycProvider` | RENIEC + OCR + liveness | **Real**: microservicio FastAPI existente; solo onboarding (HU01). |
| `DocumentLookupProvider` | Titular por DNI/RUC (apiinti) | **Real**: `GET /dni/{n}`, `/ruc/{n}` con `Bearer APIINTI_API_KEY` server-side (E1-T35); mock por defecto. |
| `HolderLookup` | Titular/CCI de cuenta destino (simulador interbancario D01) | Interfaz + mock determinista + HTTP con timeout/reintentos/circuit breaker (E2-T08); se consume por la fachada `transactions.service.validate_holder`, **sin endpoint HTTP** (D-07). |
| `CreditBureau` | Central de riesgo | Score/reglas simuladas. |
| `FxRateProvider` | API de tipo de cambio | Fallback a tasa semilla. |
| `InterbankGateway` | Red interbancaria / equipo par | **Simulador** con contrato propio (D01). |
| `ClearingSource` | Archivos de compensacion | Lote CSV/JSON simulado. |
| `SanctionsLists` | OFAC / World-Check / PEP | Cotejo difuso simulado. |
| `Notifications` | Push / correo / SMS | Multi-canal, mock por defecto. |

Reglas: timeouts, reintentos con backoff y circuit breaker en todos. Nunca exponer API keys al
cliente movil. Ver `docs/02-arquitectura.md` seccion 9.
