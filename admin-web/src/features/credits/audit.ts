// Auditoría visible del dictamen (F-T16; docs/13#2.3, docs/05#6.4).
// El backend (`POST /credits/applications/{id}/review`) devuelve `200`
// con el expediente resuelto; la tira de auditoría se deriva DE ESA
// RESPUESTA (cliente delgado, docs/19): `before` es siempre `IN_REVIEW`
// (la bandeja solo lista ese estado), `after` es el `status` resuelto y
// `at` es el `decided_at` del servidor (solo si falta se usa la hora
// local como respaldo, marcada igual). La UI no inventa el dictamen.

import type { LoanApplication, ReviewAudit } from "../../domain/types";
import { shortRef } from "../../domain/masking";

/** Auditoría del dictamen derivada del expediente resuelto por el servidor. */
export function toReviewAudit(decided: LoanApplication): ReviewAudit {
  return {
    applicationShortRef: shortRef(decided.id),
    actorShortRef: "analista",
    at: decided.decided_at ?? new Date().toISOString(),
    before: "IN_REVIEW",
    after: decided.status === "REJECTED" ? "REJECTED" : "APPROVED",
  };
}
