// Pruebas del helper de auditoría de la feature de crédito (F-T16).
// La tira visible se deriva de la RESPUESTA del servidor (cliente
// delgado, docs/19): `after` = `status` resuelto, `at` = `decided_at`.

import { describe, expect, it } from "vitest";
import { toReviewAudit } from "./audit";
import { leaksRaw, shortRef } from "../../domain/masking";
import type { LoanApplication } from "../../domain/types";

const APP_ID = "11111111-1111-4111-8111-111111111111";

function decided(over: Partial<LoanApplication> = {}): LoanApplication {
  return {
    id: APP_ID,
    user_id: "22222222-2222-4222-8222-222222222222",
    product_id: "33333333-3333-4333-8333-333333333333",
    requested_amount_minor: 1_000_000,
    term_months: 12,
    status: "APPROVED",
    bureau_authorized: true,
    bureau_authorized_at: "2026-10-08T10:24:00+00:00",
    score: 720,
    dti_ratio: "0.38",
    total_income_minor: 350000,
    total_debt_minor: 133000,
    decision_reason: "Verificado sustento de ingresos.",
    created_at: "2026-10-09T11:00:00+00:00",
    updated_at: "2026-10-09T12:00:00+00:00",
    decided_at: "2026-10-09T12:00:00+00:00",
    ...over,
  };
}

describe("toReviewAudit (auditoría del dictamen desde el servidor)", () => {
  it("usa el status y el decided_at que devuelve el backend", () => {
    const audit = toReviewAudit(decided());
    expect(audit.before).toBe("IN_REVIEW");
    expect(audit.after).toBe("APPROVED");
    expect(audit.at).toBe("2026-10-09T12:00:00+00:00");
    expect(audit.applicationShortRef).toBe(shortRef(APP_ID));
  });

  it("refleja el rechazo del servidor", () => {
    const audit = toReviewAudit(decided({ status: "REJECTED" }));
    expect(audit.after).toBe("REJECTED");
  });

  it("la referencia corta nunca expone el UUID crudo", () => {
    const audit = toReviewAudit(decided());
    expect(leaksRaw(audit.applicationShortRef, APP_ID)).toBe(false);
    expect(audit.applicationShortRef).not.toContain(APP_ID);
  });
});
