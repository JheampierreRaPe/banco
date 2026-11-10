// Páginas mínimas del shell (F-T12 corte 1): 403 y 404.
// Sin PII ni tokens; solo mensajes accionables (docs/20#7).

export function ForbiddenPage({ hint }: { hint?: string }) {
  return (
    <div className="denied-wrap">
      <h1>Acceso denegado (403)</h1>
      <p>{hint ?? "Tu usuario no tiene rol para esta vista (403 NOT_AUTHORIZED)."}</p>
    </div>
  );
}

export function NotFoundPage() {
  return (
    <div className="denied-wrap">
      <h1>Página no encontrada (404)</h1>
      <p>La ruta no existe en el panel. Usa el menú lateral para navegar.</p>
    </div>
  );
}
