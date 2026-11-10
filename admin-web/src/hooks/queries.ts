// Queries del panel (TanStack Query). Cliente delgado: sin caché
// persistente, sin recálculos; `staleTime` corto para la bandeja.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { listProducts, listReviewQueue, reviewApplication } from "../api/credits";
import type { LoanApplication, PageMeta, ReviewDecision } from "../domain/types";

export const QUEUE_KEY = ["credits", "review-queue"] as const;
export const PRODUCTS_KEY = ["credits", "products"] as const;

export interface QueueData {
  items: LoanApplication[];
  meta: PageMeta;
  hasMore: boolean;
}

// La bandeja se refresca sola cada 30s SOLO cuando la última carga fue buena:
// así una sesión vencida (401) no entra en bucle de peticiones; el analista
// sale con el botón "Volver a ingresar" (signOut).
export function useReviewQueue(token: string | null, limit = 50) {
  return useQuery({
    queryKey: [...QUEUE_KEY, { limit }],
    queryFn: () => listReviewQueue(token as string, limit),
    enabled: token !== null,
    staleTime: 15_000,
    refetchInterval: (query) => (query.state.status === "success" ? 30_000 : false),
    placeholderData: (prev) => prev,
    retryDelay: 250,
    retry: (count, err) =>
      err instanceof ApiError && (err.status === 401 || err.status === 403) ? false : count < 1,
  });
}

export function useProducts(token: string | null) {
  return useQuery({
    queryKey: PRODUCTS_KEY,
    queryFn: () => listProducts(token as string),
    enabled: token !== null,
    staleTime: 5 * 60_000,
    retry: 1,
  });
}

interface ReviewVars {
  token: string;
  applicationId: string;
  decision: ReviewDecision;
  reason: string;
}

export function useReviewMutation() {
  const client = useQueryClient();
  return useMutation<LoanApplication, ApiError, ReviewVars>({
    mutationFn: (vars) => reviewApplication(vars.token, vars.applicationId, vars.decision, vars.reason),
    onSuccess: (decided) => {
      // El expediente sale de la bandeja (CA-04) sin esperar al servidor.
      // `setQueriesData` cubre todas las variantes de la clave (paginación).
      client.setQueriesData<QueueData>({ queryKey: QUEUE_KEY }, (prev) => {
        if (!prev) return prev;
        const items = prev.items.filter((a) => a.id !== decided.id);
        return {
          items,
          meta: { ...prev.meta, total: Math.max(0, prev.meta.total - 1) },
          hasMore: prev.hasMore,
        };
      });
    },
  });
}
