// Tipos del expediente de crédito (E3-T10).
// Espejo mínimo de `docs/05#6.4` + `ApplicationData` del backend (E3-T08).
// Sin persistencia local de PII: estos objetos viven solo en memoria
// (caché de TanStack Query) y se muestran siempre enmascarados.

export type ApplicationStatus =
  | "DRAFT"
  | "SUBMITTED"
  | "IN_EVALUATION"
  | "APPROVED"
  | "REJECTED"
  | "IN_REVIEW"
  | "EXPIRED";

export type ReviewDecision = "APPROVED" | "REJECTED";

export interface LoanApplication {
  id: string;
  user_id: string;
  product_id: string;
  requested_amount_minor: number;
  term_months: number;
  status: ApplicationStatus;
  bureau_authorized: boolean;
  bureau_authorized_at: string | null;
  score: number | null;
  dti_ratio: string | null;
  total_income_minor: number | null;
  total_debt_minor: number | null;
  decision_reason: string | null;
  created_at: string | null;
  updated_at: string | null;
  decided_at: string | null;
}

export interface PageMeta {
  total: number;
  limit: number;
  offset: number;
}

export interface ProductRef {
  id: string;
  code: string;
  name: string;
}

// Auditoría visible del dictamen (docs/13#2.3: usuario, hora, antes/después).
export interface ReviewAudit {
  applicationShortRef: string;
  actorShortRef: string;
  at: string;
  before: ApplicationStatus;
  after: ReviewDecision;
}
