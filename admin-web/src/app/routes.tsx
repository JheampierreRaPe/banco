// Router del panel interno (F-T12 corte 2).
// - Las 6 rutas (`/operations`, `/fraud`, `/compliance`, `/credits`, `/admin`,
//   `/audit`) derivan de `MENU` (matriz docs/13#2.2): `allow` = roles de la
//   entrada; la unión de roles del JWT ve la unión de rutas (Opción A).
// - Guard por ruta: sin token -> `SessionGate`; rol incorrecto -> `403`
//   (`RoleGuard`/`ForbiddenPage`); ruta inexistente -> `NotFoundPage` (404).
//   `/` redirige a la primera ruta permitida (lo resuelve `Shell` en `App.tsx`;
//   si no hay ninguna, 403 "sin rol del panel").
// - Solo `/credits` tiene pantalla real (bootstrap E3-T10); el resto muestra
//   `SoonPage` hasta que lleguen `F-T13`..`F-T18`. La autoridad real es el
//   backend; el router solo es gate de UI. Sin PII ni tokens en logs.

import { useSession } from "../auth/session";
import { SessionGate } from "../components/SessionGate";
import { CreditsPage } from "./CreditsPage";
import { NotFoundPage, ForbiddenPage } from "./pages";
import { RoleGuard } from "./RoleGuard";
import { MENU } from "./roles";
import type { ComponentType } from "react";

export interface RouteEntry {
  key: string;
  label: string;
  path: string;
  /** Roles permitidos (comparación normalizada en `RoleGuard`/`hasRole`). */
  allow: string[];
  /** Brief que aporta la pantalla de negocio. */
  task: string;
  Component: ComponentType;
}

/** Placeholder de módulo hasta `F-T13`..`F-T18` (sin PII, mensaje accionable). */
export function SoonPage({ title, task }: { title: string; task: string }) {
  return (
    <div className="state-box">
      <h1>{title}</h1>
      <p>
        Próximamente — la pantalla de negocio llega en {task} (ver docs/13#2.2).
        Tu rol tiene acceso a este módulo.
      </p>
    </div>
  );
}

function soonComponent(label: string, task: string): ComponentType {
  return function Soon() {
    return <SoonPage title={label} task={task} />;
  };
}

const BUSINESS_TASKS: Record<string, string> = {
  operations: "F-T13",
  fraud: "F-T14",
  compliance: "F-T15",
  credits: "F-T16",
  admin: "F-T17",
  audit: "F-T18",
};

export const ROUTES: RouteEntry[] = MENU.map((entry) => ({
  key: entry.key,
  label: entry.label,
  path: entry.path,
  allow: entry.roles,
  task: BUSINESS_TASKS[entry.key] ?? "F-T12",
  Component:
    entry.key === "credits"
      ? CreditsPage
      : soonComponent(entry.label, BUSINESS_TASKS[entry.key] ?? "F-T12"),
}));

/** Entrada de ruta por `path` exacto; `null` si no existe (404). */
export function resolveRoute(path: string): RouteEntry | null {
  return ROUTES.find((r) => r.path === path) ?? null;
}

export function PanelRoutes({ path }: { path: string }) {
  const { session } = useSession();

  // Sin token -> sesión (puerta de login con JWT pegado, D-S4-02).
  if (!session) return <SessionGate />;

  const route = resolveRoute(path);
  // `/` sin rutas permitidas = usuario sin rol del panel -> 403.
  if (!route) {
    return path === "/" ? (
      <ForbiddenPage hint="Tu usuario no tiene rol del panel (403 NOT_AUTHORIZED)." />
    ) : (
      <NotFoundPage />
    );
  }

  return (
    <RoleGuard
      allow={route.allow}
      hint={`Tu usuario no tiene rol para ${route.label} (403 NOT_AUTHORIZED).`}
    >
      <route.Component />
    </RoleGuard>
  );
}
