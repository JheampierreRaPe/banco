// Sesión del analista (RBAC de lectura, docs/13#2.2 + matriz de accesos).
// - El analista pega su JWT (emitido por el backend con claim `roles`).
// - El panel solo lee `sub` y `roles` del payload (sin verificar firma:
//   la verificación es del servidor; aquí solo es gate de UI).
// - Sin rol `analyst`/`credit_analyst` -> pantalla denegada (equivale al
//   `403 NOT_AUTHORIZED` del backend).
// - El token vive en memoria + `sessionStorage` (separado de la app móvil);
//   jamás se loguea ni se guarda PII.

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

export const ANALYST_ROLES = new Set(["analyst", "credit_analyst"]);

const STORAGE_KEY = "admin-web.session-token";

export interface Session {
  token: string;
  subjectShortRef: string;
  roles: string[];
  isAnalyst: boolean;
}

interface SessionContextValue {
  session: Session | null;
  signIn: (token: string) => { ok: boolean; error?: string };
  signOut: () => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

function decodePayload(token: string): { sub?: unknown; roles?: unknown } | null {
  const parts = token.split(".");
  if (parts.length < 2) return null;
  try {
    const json = atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json) as { sub?: unknown; roles?: unknown };
  } catch {
    return null;
  }
}

function shortTail(value: string): string {
  const clean = value.replace(/-/g, "");
  return `••••${clean.slice(-4) || "----"}`;
}

export function buildSession(token: string): Session | null {
  const payload = decodePayload(token.trim());
  if (!payload || typeof payload.sub !== "string" || !payload.sub) return null;
  const roles = Array.isArray(payload.roles)
    ? payload.roles.filter((r): r is string => typeof r === "string")
    : [];
  const lowered = new Set(roles.map((r) => r.toLowerCase()));
  const isAnalyst = [...ANALYST_ROLES].some((r) => lowered.has(r));
  return { token: token.trim(), subjectShortRef: shortTail(payload.sub), roles, isAnalyst };
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(() => {
    try {
      const stored = sessionStorage.getItem(STORAGE_KEY);
      return stored ? buildSession(stored) : null;
    } catch {
      return null;
    }
  });

  const signIn = useCallback((token: string) => {
    const next = buildSession(token);
    if (!next) return { ok: false as const, error: "Token inválido. Pega un JWT válido." };
    try {
      sessionStorage.setItem(STORAGE_KEY, next.token);
    } catch {
      // Sesión solo en memoria si el almacenamiento falla.
    }
    setSession(next);
    return { ok: true as const };
  }, []);

  const signOut = useCallback(() => {
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      // Nada que limpiar.
    }
    setSession(null);
  }, []);

  const value = useMemo(() => ({ session, signIn, signOut }), [session, signIn, signOut]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession fuera del SessionProvider");
  return ctx;
}
