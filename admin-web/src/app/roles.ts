// Matriz de roles y menú del panel (F-T12 corte 1; matriz docs/13#2.2).
// El corte 2 cablea rutas/guards por rol; aquí queda la base (roles, menú,
// normalización) que consumen `AppShell` y `RoleGuard`.

import { normalizeRole } from "../auth/session";

export const CREDIT_ROLES = ["credit_analyst", "analyst"];

export const ROLE_LABELS: Record<string, string> = {
  operations_analyst: "Operaciones",
  fraud_analyst: "Fraude",
  compliance_officer: "Cumplimiento",
  credit_analyst: "Créditos",
  analyst: "Créditos",
  security_admin: "Administración",
  auditor: "Auditoría",
};

export interface MenuEntry {
  key: string;
  label: string;
  path: string;
  /** Roles que pueden ver la entrada (normalizados al comparar). */
  roles: string[];
  /** `ready`: implementada; `soon`: placeholder "próximamente" (corte 2+). */
  status: "ready" | "soon";
}

export const MENU: MenuEntry[] = [
  { key: "operations", label: "Operaciones", path: "/operations", roles: ["operations_analyst"], status: "soon" },
  { key: "fraud", label: "Fraude", path: "/fraud", roles: ["fraud_analyst"], status: "soon" },
  { key: "compliance", label: "Cumplimiento", path: "/compliance", roles: ["compliance_officer"], status: "soon" },
  { key: "credits", label: "Créditos", path: "/credits", roles: CREDIT_ROLES, status: "ready" },
  { key: "admin", label: "Administración", path: "/admin", roles: ["security_admin"], status: "soon" },
  { key: "audit", label: "Auditoría", path: "/audit", roles: ["auditor"], status: "soon" },
];

/** Entradas visibles para los roles de la sesión (comparación normalizada). */
export function visibleMenu(roles: string[]): MenuEntry[] {
  const owned = new Set(roles.map(normalizeRole));
  return MENU.filter((entry) => entry.roles.some((r) => owned.has(normalizeRole(r))));
}

/** Etiqueta corta del rol para el selector/topbar (sin PII). */
export function roleLabel(role: string): string {
  return ROLE_LABELS[normalizeRole(role)] ?? role;
}
