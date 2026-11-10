// Guarda RBAC mínima del panel (F-T12 corte 1; base para el corte 2).
// Sin sesión o sin rol permitido -> `ForbiddenPage` (equivale al 403;
// la autoridad real es el backend). No loguea tokens ni PII.

import type { ReactNode } from "react";
import { useSession } from "../auth/session";
import { ForbiddenPage } from "./pages";

interface Props {
  allow: string[];
  hint?: string;
  children: ReactNode;
}

export function RoleGuard({ allow, hint, children }: Props) {
  const { hasRole } = useSession();
  if (!hasRole(...allow)) return <ForbiddenPage hint={hint} />;
  return <>{children}</>;
}
