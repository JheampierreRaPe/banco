// Enmascaramiento y formato de presentación (docs/13#2.3, docs/16 regla 9).
// El backend de analista devuelve UUIDs crudos (`user_id`, `product_id`);
// el panel JAMÁS los renderiza completos. Los montos los provee el
// servidor en céntimos (cliente delgado, docs/19): aquí solo se formatean
// a `S/ 9,999.99` para mostrar, sin calcular ni redondear negocio.

const MASK = "••••";

/** Últimos 4 caracteres de una referencia, con prefijo enmascarado. */
export function maskRef(value: string | null | undefined): string {
  if (!value) return `${MASK}----`;
  const tail = value.replace(/-/g, "").slice(-4) || "----";
  return `${MASK}${tail}`;
}

/** Referencia corta del expediente para títulos (nunca el id completo). */
export function shortRef(id: string): string {
  const clean = id.replace(/-/g, "");
  return `CR-${clean.slice(0, 4).toUpperCase()}${clean.slice(4, 8).toUpperCase()}`;
}

/** `true` si el texto contiene el valor en claro (guardia de PII en tests). */
export function leaksRaw(text: string, raw: string | null | undefined): boolean {
  if (!raw || raw.length < 8) return false;
  return text.includes(raw);
}

/** Céntimos (int) -> `S/ 9,999.99`. Solo presentación. */
export function formatPEN(minor: number | null | undefined): string {
  if (minor === null || minor === undefined || !Number.isFinite(minor)) return "S/ —";
  const soles = minor / 100;
  return `S/ ${soles.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** `dti_ratio` (string decimal del servidor) -> `38%`. */
export function formatDtiPercent(dtiRatio: string | null | undefined): string {
  if (dtiRatio === null || dtiRatio === undefined) return "DTI —";
  const n = Number(dtiRatio);
  if (!Number.isFinite(n)) return "DTI —";
  return `DTI ${Math.round(n * 100)}%`;
}

/** Minutos transcurridos desde un ISO (para "hace 12 min"). */
export function timeAgo(iso: string | null | undefined, nowMs = Date.now()): string {
  if (!iso) return "fecha —";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "fecha —";
  const mins = Math.max(0, Math.round((nowMs - t) / 60000));
  if (mins < 1) return "ahora mismo";
  if (mins < 60) return `hace ${mins} min`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `hace ${hours} h`;
  return `hace ${Math.round(hours / 24)} d`;
}
