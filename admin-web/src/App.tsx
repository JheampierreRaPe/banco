// Panel interno (F-T12 corte 2): shell + router real con guards RBAC.
// - `/` redirige a la primera ruta permitida (unión de roles del JWT).
// - Sin token -> `SessionGate`; rol incorrecto -> `403`; ruta inexistente -> `404`.
// - El selector de rol solo cambia la vista inicial (Opción A); la unión manda.
// - `401` (sesión expirada) vuelve a sesión vía `signOut` en la vista de crédito.

import { useCallback, useEffect, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useSession } from "./auth/session";
import { AppShell } from "./app/AppShell";
import { PanelRoutes } from "./app/routes";
import { firstAllowedPath, firstPathForRole } from "./app/roles";

function Shell({ initialPath }: { initialPath?: string }) {
  const { session } = useSession();
  const [path, setPath] = useState<string>(
    () => initialPath ?? window.location.pathname,
  );

  useEffect(() => {
    if (initialPath !== undefined) return;
    const onPopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [initialPath]);

  const navigate = useCallback(
    (to: string) => {
      if (initialPath === undefined) window.history.pushState({}, "", to);
      setPath(to);
    },
    [initialPath],
  );

  const handleRoleChange = useCallback(
    (role: string) => {
      const target = firstPathForRole(role);
      if (target) navigate(target);
    },
    [navigate],
  );

  // `/` -> primera ruta permitida de la unión de roles (Opción A).
  const effective =
    path === "/" && session ? (firstAllowedPath(session.roles) ?? path) : path;

  return (
    <AppShell activePath={effective} onNavigate={navigate} onRoleChange={handleRoleChange}>
      <PanelRoutes path={effective} />
    </AppShell>
  );
}

export function App({ initialPath }: { initialPath?: string } = {}) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { refetchOnWindowFocus: false } },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <Shell initialPath={initialPath} />
    </QueryClientProvider>
  );
}
