// Sesión del panel interno (F-T12, corte 1; D-S4-02: JWT pegado, sin login real).
// - El operador pega su JWT (emitido por el backend con claims `sub` + `roles`).
// - El panel solo lee `sub` y `roles` del payload (sin verificar firma:
//   la verificación es del servidor; aquí solo es gate de UI).
// - Roles canónicos (docs/13#2.2): `operations_analyst`, `fraud_analyst`,
//   `compliance_officer`, `credit_analyst`, `security_admin`, `auditor`.
//   Se acepta el alias histórico `analyst` (= `credit_analyst`, bootstrap E3-T10).
// - El token vive en memoria + `sessionStorage` con clave propia del panel
//   (separada de la app móvil); jamás se loguea ni se guarda PII.

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";

/** Roles canónicos del panel (docs/13#2.2). */
export const PANEL_ROLES = [
  "operations_analyst",
  "fraud_analyst",
  "compliance_officer",
  "credit_analyst",
  "security_admin",
  "auditor",
] as const;

export type PanelRole = (typeof PANEL_ROLES)[number];

/** Alias históricos aceptados en el claim `roles` del JWT. */
const ROLE_ALIASES: Record<string, string> = {
  analyst: "credit_analyst",
};

/** Roles con acceso a la vista de crédito (bootstrap E3-T10). */
export const ANALYST_ROLES = new Set(["analyst", "credit_analyst"]);

export const STORAGE_KEY = "admin-web.session-token";

export interface Session {
  token: string;
  /** `sub` en claro SOLO en memoria (nunca renderizarlo directo; usar `subjectShortRef`). */
  sub: string;
  subjectShortRef: string;
  roles: string[];
  /** Compatibilidad con la vista de crédito (E3-T10). */
  isAnalyst: boolean;
}

interface SessionContextValue {
  session: Session | null;
  signIn: (token: string) => { ok: boolean; error?: string };
  signOut: () => void;
  /** `true` si la sesión tiene alguno de los roles (insensible a mayúsculas; acepta alias). */
  hasRole: (...roles: string[]) => boolean;
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

/** Normaliza un rol del JWT (minúsculas + alias históricos). */
export function normalizeRole(role: string): string {
  const lower = role.trim().toLowerCase();
  return ROLE_ALIASES[lower] ?? lower;
}

export function buildSession(token: string): Session | null {
  const payload = decodePayload(token.trim());
  if (!payload || typeof payload.sub !== "string" || !payload.sub) return null;
  const roles = Array.isArray(payload.roles)
    ? payload.roles.filter((r): r is string => typeof r === "string")
    : [];
  const lowered = new Set(roles.map((r) => r.toLowerCase()));
  const isAnalyst = [...ANALYST_ROLES].some((r) => lowered.has(r));
  return {
    token: token.trim(),
    sub: payload.sub,
    subjectShortRef: shortTail(payload.sub),
    roles,
    isAnalyst,
  };
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

  const hasRole = useCallback(
    (...roles: string[]) => {
      if (!session) return false;
      const owned = new Set(session.roles.map(normalizeRole));
      return roles.some((r) => owned.has(normalizeRole(r)));
    },
    [session],
  );

  const value = useMemo(
    () => ({ session, signIn, signOut, hasRole }),
    [session, signIn, signOut, hasRole],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession fuera del SessionProvider");
  return ctx;
}
