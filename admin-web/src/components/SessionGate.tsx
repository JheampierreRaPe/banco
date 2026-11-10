import { useState } from "react";
import type { FormEvent } from "react";
import { PANEL_ROLES, normalizeRole, useSession } from "../auth/session";

// Puerta de sesión + RBAC del panel (F-T12 corte 2; matriz docs/13#2.2).
// - Sin sesión: pegar JWT (D-S4-02, sin login real de panel).
// - Con sesión sin ningún rol del panel: denegada (equivale al 403).
// - Con sesión con rol del panel: `null` (el router decide ruta/guard).
// Sin PII ni tokens: solo `subjectShortRef`, jamás el `sub` crudo.
export function SessionGate() {
  const { session, signIn, signOut } = useSession();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (session) {
    const panelRoles = new Set(PANEL_ROLES.map((r) => normalizeRole(r)));
    const hasPanelRole = session.roles.some((r) => panelRoles.has(normalizeRole(r)));
    if (!hasPanelRole) {
      return (
        <div className="denied-wrap">
          <h1>Acceso denegado</h1>
          <p>Tu usuario no tiene rol del panel (403 NOT_AUTHORIZED).</p>
          <p>Sesión: {session.subjectShortRef}</p>
          <button type="button" className="btn btn-secondary" onClick={signOut}>
            Cerrar sesión
          </button>
        </div>
      );
    }
    return null;
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const res = signIn(token);
    setError(res.ok ? null : (res.error ?? "Token inválido."));
  };
  return (
    <div className="session-wrap">
      <h1>Panel interno — Sesión</h1>
      <p className="subtitle">Pega tu JWT para abrir el panel (sesión separada de la app móvil).</p>
      <form onSubmit={onSubmit}>
        <label htmlFor="jwt">Token de sesión (Bearer, sesión separada de la app móvil)</label>
        <textarea
          id="jwt"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="eyJhbGciOi..."
          aria-label="Token de sesión"
        />
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        <div className="btn-row">
          <button type="submit" className="btn btn-primary">
            Ingresar
          </button>
        </div>
      </form>
    </div>
  );
}
