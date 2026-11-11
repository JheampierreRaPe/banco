// Vista de crédito bajo el shell del panel (F-T16).
// Conexión delgada: la feature vive en `features/credits` (`CreditsPanel`:
// bandeja IN_REVIEW + detalle enmascarado + dictamen con confirmación);
// esta página solo la monta bajo el `RoleGuard` de `routes.tsx`
// (`credit_analyst`/`analyst`). Sin PII/tokens en logs.

import { CreditsPanel } from "../features/credits";

export function CreditsPage() {
  return <CreditsPanel />;
}
