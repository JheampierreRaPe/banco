// Chip de estado (F-T12 corte 1). Mapea estados del servidor a tonos de
// los tokens "Eucalipto y Ocre" (docs/20#3); no inventa estados de negocio.

const SUCCESS = new Set(["APPROVED", "ACTIVE", "SUCCESS", "RESOLVED", "COMPLETED"]);
const DANGER = new Set(["REJECTED", "BLOCKED", "FAILED", "DENIED"]);
const WARNING = new Set(["IN_REVIEW", "PENDING", "ON_HOLD", "EN_REVISION"]);

export function StatusChip({ status, label }: { status: string; label?: string }) {
  const upper = status.toUpperCase();
  const tone = SUCCESS.has(upper) ? "ok" : DANGER.has(upper) ? "bad" : WARNING.has(upper) ? "warn" : "info";
  return (
    <span className={`pill pill-${tone}`} data-status={status}>
      {label ?? status}
    </span>
  );
}
