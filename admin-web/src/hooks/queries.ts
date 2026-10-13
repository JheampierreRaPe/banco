// Queries del panel (TanStack Query). Cliente delgado: sin caché
// persistente, sin recálculos; `staleTime` corto para la bandeja.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { listProducts, listReviewQueue, reviewApplication } from "../api/credits";
import type { LoanApplication, ReviewDecision } from "../domain/types";

export const QUEUE_KEY = ["credits", "review-queue"] as const;
export const PRODUCTS_KEY = ["credits", "products"] as const;

export function useReviewQueue(token: string | null) {
  return useQuery({
    queryKey: QUEUE_KEY,
    queryFn: () => listReviewQueue(token as string),
    enabled: token !== null,
    staleTime: 15_000,
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
      client.setQueryData<{ items: LoanApplication[]; meta: { total: number; limit: number; offset: number } }>(
        QUEUE_KEY,
        (prev) => {
          if (!prev) return prev;
          const items = prev.items.filter((a) => a.id !== decided.id);
          return { items, meta: { ...prev.meta, total: Math.max(0, prev.meta.total - 1) } };
        },
      );
    },
  });
}
