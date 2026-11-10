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

// La bandeja (`status=IN_REVIEW`) NO expone un total real: el backend
// devuelve `meta.total = len(items)` (tamaño de página) y topa `limit` a
// 1..100 (`backend/.../credits/api/applications.py`). Por eso, para "cargar
// más" se pagina por `offset` en bloques y `hasMore` indica si puede haber
// filas siguientes (última página llena -> contador "50+").
const REVIEW_PAGE_MAX = 100;

export async function listReviewQueue(
  token: string,
  limit = 50,
): Promise<{ items: LoanApplication[]; meta: PageMeta; hasMore: boolean }> {
  const items: LoanApplication[] = [];
  let offset = 0;
  let hasMore = false;
  while (items.length < limit) {
    const chunk = Math.min(REVIEW_PAGE_MAX, limit - items.length);
    const { data } = await apiFetch<LoanApplication[]>(
      `/credits/applications?status=IN_REVIEW&limit=${chunk}&offset=${offset}`,
      token,
    );
    const page = Array.isArray(data) ? data : [];
    items.push(...page);
    offset += page.length;
    if (page.length < chunk) {
      hasMore = false;
      break;
    }
    hasMore = true;
    if (page.length === 0) break;
  }
  return {
    items,
    meta: { total: items.length, limit, offset: 0 },
    hasMore,
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
