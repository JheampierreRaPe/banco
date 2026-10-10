import { formatDtiPercent, formatPEN, maskRef, shortRef, timeAgo } from "../domain/masking";
import type { LoanApplication, ProductRef } from "../domain/types";

interface Props {
  items: LoanApplication[];
  total: number;
  selectedId: string | null;
  onSelect: (id: string) => void;
  productName: (id: string) => string | null;
}

// Bandeja EN_REVISION (filtro fijo `status=IN_REVIEW`, aviso de enmascarado).
// Cada fila muestra solo datos enmascarados: jamás UUID/documento en claro.
export function QueueList({ items, total, selectedId, onSelect, productName }: Props) {
  return (
    <section aria-label="Bandeja en revisión manual">
      <h1>En revisión manual</h1>
      <p className="subtitle">Filtro: status = IN_REVIEW · Bandeja ({total})</p>
      <p className="notice-mask">Datos enmascarados.</p>
      {items.map((app) => {
        const ref = shortRef(app.id);
        const product = productName(app.product_id) ?? `Producto ${maskRef(app.product_id)}`;
        return (
          <article key={app.id} className={`card${selectedId === app.id ? " selected" : ""}`}>
            <button type="button" className="row-open" onClick={() => onSelect(app.id)}>
              <p className="row-title">
                {ref} - {product} {formatPEN(app.requested_amount_minor)}
              </p>
              <p className="row-sub">
                Solicitante {maskRef(app.user_id)} - Doc. {maskRef(app.user_id)} -{" "}
                {timeAgo(app.created_at)}
              </p>
              <span className="pill">
                IN_REVIEW - {formatDtiPercent(app.dti_ratio)}
              </span>
            </button>
          </article>
        );
      })}
    </section>
  );
}

export function productNameResolver(products: ProductRef[] | undefined) {
  const byId = new Map((products ?? []).map((p) => [p.id, p.name]));
  return (id: string): string | null => byId.get(id) ?? null;
}
