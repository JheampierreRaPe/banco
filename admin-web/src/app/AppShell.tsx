// Shell del panel interno (F-T12 corte 1): topbar + sidebar + contenido.
// - Sidebar con navegación por rol (entradas no implementadas: deshabilitadas
//   con "próximamente"); colapsable en pantallas pequeñas (botón + media query).
// - Topbar con selector de rol visible: `select` si hay varios roles,
//   chip si hay uno solo. Muestra `subjectShortRef` (nunca el `sub` crudo).
// - `activePath` + `onNavigate` dejan la base lista para el router del corte 2
//   (hoy todo el contenido es la vista de crédito bajo `/credits`).

import { useState } from "react";
import type { ReactNode } from "react";
import { useSession } from "../auth/session";
import { MENU, roleLabel } from "./roles";

interface Props {
  activePath?: string;
  onNavigate?: (path: string) => void;
  children: ReactNode;
}

export function AppShell({ activePath = "/credits", onNavigate, children }: Props) {
  const { session, signOut } = useSession();
  const [navOpen, setNavOpen] = useState(true);
  const roles = session?.roles ?? [];
  const [activeRole, setActiveRole] = useState<string | null>(null);
  const selectedRole = activeRole && roles.includes(activeRole) ? activeRole : (roles[0] ?? null);

  return (
    <div className={`shell${navOpen ? "" : " shell-nav-collapsed"}`}>
      <header className="shell-topbar">
        <button
          type="button"
          className="btn btn-secondary shell-nav-toggle"
          aria-label={navOpen ? "Ocultar navegación" : "Mostrar navegación"}
          aria-expanded={navOpen}
          onClick={() => setNavOpen((v) => !v)}
        >
          ☰
        </button>
        <span className="shell-brand">Panel interno — Banca Online</span>
        {session && (
          <div className="shell-session">
            {roles.length > 1 && selectedRole ? (
              <label className="shell-role-pick">
                Rol activo{" "}
                <select
                  aria-label="Rol activo"
                  value={selectedRole}
                  onChange={(e) => setActiveRole(e.target.value)}
                >
                  {roles.map((r) => (
                    <option key={r} value={r}>
                      {roleLabel(r)}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <span className="shell-role-chip">
                Rol: {selectedRole ? roleLabel(selectedRole) : "—"}
              </span>
            )}
            <span className="shell-sub" title="Sesión del panel (separada de la app móvil)">
              {session.subjectShortRef}
            </span>
          </div>
        )}
      </header>
      <div className="shell-body">
        <aside
          className="shell-sidebar"
          aria-label="Navegación del panel"
          aria-hidden={!navOpen}
        >
          <nav>
            <ul className="shell-nav">
              {MENU.map((entry) => {
                const active = entry.path === activePath;
                const soon = entry.status === "soon";
                return (
                  <li key={entry.key}>
                    {soon ? (
                      <span
                        className={`shell-nav-item disabled${active ? " active" : ""}`}
                        aria-disabled="true"
                        title="Próximamente"
                      >
                        {entry.label} <span className="nav-soon">próximamente</span>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className={`shell-nav-link${active ? " active" : ""}`}
                        aria-current={active ? "page" : undefined}
                        onClick={() => onNavigate?.(entry.path)}
                      >
                        {entry.label}
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          </nav>
          {session && (
            <div className="role-foot">
              <button type="button" className="btn btn-secondary btn-logout" onClick={signOut}>
                Cerrar sesión
              </button>
            </div>
          )}
        </aside>
        <main className="shell-content">{children}</main>
      </div>
    </div>
  );
}
