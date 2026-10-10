// Endpoints de analista consumidos (E3-T08, docs/05#6.4):
// - `GET /credits/applications?status=IN_REVIEW` -> bandeja.
// - `POST /credits/applications/{id}/review` -> dictamen manual.
// - `GET /credits/products` -> nombres de producto (solo mapeo id->nombre).
//
// NOTA (gap G52, docs/18): `GET /credits/applications/{id}` es solo del
// titular (ajeno -> `404` neutro), así que el detalle del analista se
// construye con la fila de la bandeja (que ya trae el expediente completo
// del servidor: score, DTI, consentimiento, motivo). No se hace un segundo
// fetch que siempre fallaría con token de analista.

import { apiFetch } from "./client";
import type {
  LoanApplication,
  PageMeta,
  ProductRef,
  ReviewDecision,
} from "../domain/types";

export async function listReviewQueue(
  token: string,
): Promise<{ items: LoanApplication[]; meta: PageMeta }> {
  const { data, meta } = await apiFetch<LoanApplication[]>(
    "/credits/applications?status=IN_REVIEW&limit=50&offset=0",
    token,
  );
  return {
    items: Array.isArray(data) ? data : [],
    meta: {
      total: Number(meta.total ?? (Array.isArray(data) ? data.length : 0)),
      limit: Number(meta.limit ?? 50),
      offset: Number(meta.offset ?? 0),
    },
  };
}

export async function reviewApplication(
  token: string,
  applicationId: string,
  decision: ReviewDecision,
  reason: string,
): Promise<LoanApplication> {
  const { data } = await apiFetch<LoanApplication>(
    `/credits/applications/${applicationId}/review`,
    token,
    {
      method: "POST",
      body: JSON.stringify({ decision, reason }),
    },
  );
  return data;
}

export async function listProducts(token: string): Promise<ProductRef[]> {
  const { data } = await apiFetch<ProductRef[]>("/credits/products", token);
  return Array.isArray(data) ? data : [];
}
