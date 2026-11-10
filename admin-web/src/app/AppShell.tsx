// Shell del panel interno (F-T12 corte 2): topbar + sidebar + contenido.
// - Sidebar con la UNIÓN de rutas de todos los roles del JWT (`visibleMenu`);
//   todas las entradas navegan (Opción A del dueño). Las aún sin pantalla de
//   negocio llevan la etiqueta "próximamente" y abren su placeholder
//   (`SoonPage` hasta `F-T13`..`F-T18`). Colapsable (botón + media query).
// - Topbar con selector de rol visible: `select` si hay varios roles,
//   chip si hay uno solo. El selector solo resalta/cambia la vista inicial
//   (`onRoleChange` -> primera ruta de ese rol); NO restringe permisos.
//   Muestra `subjectShortRef` (nunca el `sub` crudo).

import { useState } from "react";
import type { ReactNode } from "react";
import { useSession } from "../auth/session";
import { visibleMenu, roleLabel } from "./roles";

interface Props {
  activePath?: string;
  onNavigate?: (path: string) => void;
  /** El selector de rol eligió vista inicial (primera ruta de ese rol). */
  onRoleChange?: (role: string) => void;
  children: ReactNode;
}

export function AppShell({ activePath = "/credits", onNavigate, onRoleChange, children }: Props) {
  const { session, signOut } = useSession();
  const [navOpen, setNavOpen] = useState(true);
  const roles = session?.roles ?? [];
  const entries = visibleMenu(roles);
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
                  onChange={(e) => {
                    setActiveRole(e.target.value);
                    onRoleChange?.(e.target.value);
                  }}
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
              {entries.map((entry) => {
                const active = entry.path === activePath;
                const soon = entry.status === "soon";
                return (
                  <li key={entry.key}>
                    <button
                      type="button"
                      className={`shell-nav-link${active ? " active" : ""}`}
                      aria-current={active ? "page" : undefined}
                      onClick={() => onNavigate?.(entry.path)}
                    >
                      {entry.label}
                      {soon && <span className="nav-soon">próximamente</span>}
                    </button>
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
