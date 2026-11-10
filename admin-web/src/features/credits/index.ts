// Feature de crédito del panel (F-T16). Punto de entrada público:
// la página `app/CreditsPage.tsx` solo conecta `CreditsPanel` bajo el
// `RoleGuard` de `routes.tsx` (shell/RBAC de F-T12).

export { CreditsPanel } from "./CreditsPanel";
export { toReviewAudit } from "./audit";
