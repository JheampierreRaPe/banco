import { useMemo, useState } from "react";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "./api/client";
import { useSession } from "./auth/session";
import { SessionGate } from "./components/SessionGate";
import { QueueList, productNameResolver } from "./components/QueueList";
import { ApplicationDetail } from "./components/ApplicationDetail";
import { QUEUE_KEY, useProducts, useReviewMutation, useReviewQueue } from "./hooks/queries";
import type { LoanApplication, ReviewAudit } from "./domain/types";

function decisionLabel(audit: ReviewAudit): string {
  return audit.after === "APPROVED" ? "Aprobado" : "Rechazado";
}

function AnalystView() {
  const { session, signOut } = useSession();
  const token = session?.token ?? null;
  const queue = useReviewQueue(token);
  const products = useProducts(token);
  const reviewMutation = useReviewMutation();
  const client = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [lastAudit, setLastAudit] = useState<ReviewAudit | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);

  const resolveProduct = useMemo(
    () => productNameResolver(products.data),
    [products.data],
  );

  const items = queue.data?.items ?? [];
  const total = queue.data?.meta.total ?? items.length;
  const selected: LoanApplication | null =
    items.find((a) => a.id === selectedId) ?? null;

  const removeFromQueue = (applicationId: string) => {
    client.setQueryData(QUEUE_KEY, (prev: { items: LoanApplication[]; meta: { total: number; limit: number; offset: number } } | undefined) => {
      if (!prev) return prev;
      const next = prev.items.filter((a) => a.id !== applicationId);
      return { items: next, meta: { ...prev.meta, total: Math.max(0, prev.meta.total - 1) } };
    });
    setSelectedId((cur) => (cur === applicationId ? null : cur));
  };

  const forbidden =
    denied ||
    (queue.error instanceof ApiError && queue.error.status === 403);

  if (forbidden) {
    return (
      <div className="denied-wrap">
        <h1>Acceso denegado</h1>
        <p>Tu usuario no tiene rol de analista de crédito (403 NOT_AUTHORIZED).</p>
        <button type="button" className="btn btn-secondary" onClick={signOut}>
          Cerrar sesión
        </button>
      </div>
    );
  }

  return (
    <div>
      <div className="layout">
        <aside className="sidebar" aria-label="Navegación del panel">
          <h2>Panel Créditos</h2>
          <div className="nav-item active">Bandeja IN_REVIEW ({total})</div>
          <div className="nav-item">Aprobados hoy</div>
          <div className="nav-item">Rechazados</div>
          <div className="nav-item">Auditoría</div>
          <div className="role-foot">Rol: Analista de crédito</div>
        </aside>
        <div className="main">
          <div className="queue-pane">
            {queue.isPending && (
              <div aria-label="Cargando bandeja">
                <h1>En revisión manual</h1>
                <div className="skeleton" />
                <div className="skeleton" />
                <div className="skeleton" />
              </div>
            )}
            {queue.isError && (
              <div className="state-box" role="alert">
                <h1>En revisión manual</h1>
                <p className="error-text">
                  {queue.error instanceof ApiError && queue.error.code === "NOT_AUTHENTICATED"
                    ? "Tu sesión expiró. Vuelve a ingresar."
                    : `No se pudo cargar la bandeja. ${queue.error instanceof Error ? queue.error.message : ""}`}
                </p>
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => {
                    setDenied(false);
                    void queue.refetch();
                  }}
                >
                  Reintentar
                </button>
              </div>
            )}
            {queue.isSuccess && items.length === 0 && (
              <div className="state-box">
                <h1>En revisión manual</h1>
                <p>Sin expedientes por revisar</p>
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => void queue.refetch()}
                >
                  Reintentar
                </button>
              </div>
            )}
            {queue.isSuccess && items.length > 0 && (
              <QueueList
                items={items}
                total={total}
                selectedId={selectedId}
                onSelect={(id) => {
                  setSelectedId(id);
                  setLastAudit(null);
                  setNotice(null);
                }}
                productName={resolveProduct}
              />
            )}
          </div>
          <div className="detail-pane">
            {notice && (
              <div className="audit-strip" role="alert">
                {notice}
              </div>
            )}
            {lastAudit && (
              <div className="audit-strip" role="status">
                Dictamen {decisionLabel(lastAudit)} — {lastAudit.applicationShortRef} ·{" "}
                {lastAudit.before} → {lastAudit.after} · {lastAudit.at} (auditado)
              </div>
            )}
            {queue.isSuccess && selected && (
              <ApplicationDetail
                app={selected}
                productLabel={resolveProduct(selected.product_id)}
                reviewing={reviewMutation.isPending}
                review={async (decision, reason) => {
                  if (!token) throw new Error("Sin sesión");
                  try {
                    return await reviewMutation.mutateAsync({
                      token,
                      applicationId: selected.id,
                      decision,
                      reason,
                    });
                  } catch (err) {
                    if (err instanceof ApiError && err.status === 403) setDenied(true);
                    throw err;
                  }
                }}
                onReviewed={(audit) => {
                  setNotice(null);
                  setLastAudit(audit);
                }}
                onConflict={(id) => {
                  // 409: el detalle se desmonta al salir de la bandeja; el
                  // aviso vive aquí para que el analista lo lea.
                  setNotice("El expediente ya fue decidido por otro analista. Se actualizó la bandeja.");
                  setLastAudit(null);
                  removeFromQueue(id);
                  void queue.refetch();
                }}
              />
            )}
            {queue.isSuccess && !selected && items.length > 0 && (
              <div className="state-box">
                <p>Selecciona un expediente de la bandeja para ver el detalle.</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Shell() {
  const { session } = useSession();
  return (
    <div>
      <header className="topbar">Panel Créditos — Revisión del analista</header>
      <SessionGate />
      {session?.isAnalyst && <AnalystView />}
    </div>
  );
}

export function App() {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { refetchOnWindowFocus: false } },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <Shell />
    </QueryClientProvider>
  );
}
