import { useState } from "react";
import type { FormEvent } from "react";
import { useSession } from "../auth/session";

// Puerta de sesión + RBAC (docs/13#2.2). Sin sesión: pegar JWT.
// Con sesión sin rol analista: pantalla denegada (equivale al 403).
export function SessionGate() {
  const { session, signIn, signOut } = useSession();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (session && !session.isAnalyst) {
    return (
      <div className="denied-wrap">
        <h1>Acceso denegado</h1>
        <p>Tu usuario no tiene rol de analista de crédito (403 NOT_AUTHORIZED).</p>
        <p>Sesión: {session.subjectShortRef}</p>
        <button type="button" className="btn btn-secondary" onClick={signOut}>
          Cerrar sesión
        </button>
      </div>
    );
  }

  if (!session) {
    const onSubmit = (e: FormEvent) => {
      e.preventDefault();
      const res = signIn(token);
      setError(res.ok ? null : (res.error ?? "Token inválido."));
    };
    return (
      <div className="session-wrap">
        <h1>Panel Créditos — Analista</h1>
        <p className="subtitle">Pega tu JWT de analista para abrir la bandeja EN_REVISION.</p>
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

  return null;
}
