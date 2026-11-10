import { useMemo, useState } from "react";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "./api/client";
import { useSession } from "./auth/session";
import { SessionGate } from "./components/SessionGate";
import { QueueList, productNameResolver } from "./components/QueueList";
import { ApplicationDetail } from "./components/ApplicationDetail";
import type { QueueData } from "./hooks/queries";
import { QUEUE_KEY, useProducts, useReviewMutation, useReviewQueue } from "./hooks/queries";
import type { LoanApplication, ReviewAudit } from "./domain/types";

function decisionLabel(audit: ReviewAudit): string {
  return audit.after === "APPROVED" ? "Aprobado" : "Rechazado";
}

function AnalystView() {
  const { session, signOut } = useSession();
  const token = session?.token ?? null;
  const [limit, setLimit] = useState(50);
  const queue = useReviewQueue(token, limit);
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
  const hasMore = queue.data?.hasMore ?? false;
  const selected: LoanApplication | null =
    items.find((a) => a.id === selectedId) ?? null;

  const removeFromQueue = (applicationId: string) => {
    client.setQueriesData<QueueData>({ queryKey: QUEUE_KEY }, (prev) => {
      if (!prev) return prev;
      const next = prev.items.filter((a) => a.id !== applicationId);
      return {
        items: next,
        meta: { ...prev.meta, total: Math.max(0, prev.meta.total - 1) },
        hasMore: prev.hasMore,
      };
    });
    setSelectedId((cur) => (cur === applicationId ? null : cur));
  };

  const sessionExpired =
    queue.error instanceof ApiError && queue.error.code === "NOT_AUTHENTICATED";

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
          <div className="nav-item active">Bandeja IN_REVIEW ({total}{hasMore ? "+" : ""})</div>
          <div className="nav-item disabled" aria-disabled="true" title="Próximamente">
            Aprobados hoy <span className="nav-soon">próximamente</span>
          </div>
          <div className="nav-item disabled" aria-disabled="true" title="Próximamente">
            Rechazados <span className="nav-soon">próximamente</span>
          </div>
          <div className="nav-item disabled" aria-disabled="true" title="Próximamente">
            Auditoría <span className="nav-soon">próximamente</span>
          </div>
          <div className="role-foot">
            <span>Rol: Analista de crédito</span>
            <button type="button" className="btn btn-secondary btn-logout" onClick={signOut}>
              Cerrar sesión
            </button>
          </div>
        </aside>
        <div className="main">
          <div className="queue-pane">
            <div className="queue-toolbar">
              <button
                type="button"
                className="btn btn-secondary btn-refresh"
                onClick={() => void queue.refetch()}
                disabled={queue.isFetching}
              >
                {queue.isFetching ? "Actualizando…" : "Refrescar"}
              </button>
            </div>
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
                  {sessionExpired
                    ? "Tu sesión expiró. Vuelve a ingresar."
                    : `No se pudo cargar la bandeja. ${queue.error instanceof Error ? queue.error.message : ""}`}
                </p>
                {sessionExpired ? (
                  <button type="button" className="btn btn-primary" onClick={signOut}>
                    Volver a ingresar
                  </button>
                ) : (
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
                )}
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
              <>
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
                {hasMore && (
                  <button
                    type="button"
                    className="btn btn-secondary load-more"
                    onClick={() => setLimit((cur) => cur + 50)}
                    disabled={queue.isFetching}
                  >
                    {queue.isFetching
                      ? "Cargando más…"
                      : `Cargar más (${items.length} cargados)`}
                  </button>
                )}
              </>
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
