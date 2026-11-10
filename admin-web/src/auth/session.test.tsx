// Pruebas de la sesión generalizada (F-T12 corte 1; D-S4-02 JWT pegado).
// Cubre: token válido/inválido, `sub`/`roles`, `hasRole` (+ alias y
// mayúsculas), persistencia en `sessionStorage` separado y logout.

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  ANALYST_ROLES,
  PANEL_ROLES,
  STORAGE_KEY,
  SessionProvider,
  buildSession,
  useSession,
} from "./session";

const SUB = "22222222-2222-4222-8222-222222222222";

function fakeJwt(sub: unknown = SUB, roles: unknown = ["credit_analyst"]): string {
  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256" })}.${b64({ sub, roles })}.firma`;
}

function Probe({ roles }: { roles: string[] }) {
  const { hasRole } = useSession();
  return <output data-testid="probe">{String(hasRole(...roles))}</output>;
}

describe("sesión del panel", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    sessionStorage.clear();
  });

  it("token válido: lee sub/roles y marca analista de crédito", () => {
    const s = buildSession(fakeJwt());
    expect(s).not.toBeNull();
    expect(s?.sub).toBe(SUB);
    expect(s?.roles).toEqual(["credit_analyst"]);
    expect(s?.isAnalyst).toBe(true);
    // Enmascarado: la referencia corta nunca contiene el sub crudo.
    expect(s?.subjectShortRef).toContain("••••");
    expect(s?.subjectShortRef).not.toContain(SUB);
  });

  it("token inválido o sin sub: rechaza la sesión", () => {
    expect(buildSession("no-es-jwt")).toBeNull();
    expect(buildSession("a.b.c")).toBeNull();
    expect(buildSession(fakeJwt(null))).toBeNull();
    expect(buildSession(fakeJwt(""))).toBeNull();
  });

  it("acepta los 6 roles canónicos y el alias histórico analyst", () => {
    for (const role of PANEL_ROLES) {
      expect(buildSession(fakeJwt(SUB, [role]))?.roles).toEqual([role]);
    }
    expect(buildSession(fakeJwt(SUB, ["analyst"]))?.isAnalyst).toBe(true);
    expect(ANALYST_ROLES.has("credit_analyst")).toBe(true);
  });

  it("roles no panel (vacío) no marcan analista", () => {
    const s = buildSession(fakeJwt(SUB, []));
    expect(s?.isAnalyst).toBe(false);
  });

  it("signIn persiste en sessionStorage separado; signOut lo limpia", async () => {
    const user = userEvent.setup();
    function Harness() {
      const { session, signIn, signOut } = useSession();
      return (
        <div>
          <span data-testid="state">{session ? session.subjectShortRef : "sin-sesion"}</span>
          <button type="button" onClick={() => void signIn(fakeJwt())}>
            entrar
          </button>
          <button type="button" onClick={signOut}>
            salir
          </button>
        </div>
      );
    }
    render(
      <SessionProvider>
        <Harness />
      </SessionProvider>,
    );
    expect(screen.getByTestId("state")).toHaveTextContent("sin-sesion");
    await user.click(screen.getByRole("button", { name: "entrar" }));
    expect(screen.getByTestId("state")).toHaveTextContent("••••");
    // Clave propia del panel (no colisiona con la app móvil).
    expect(STORAGE_KEY).toBe("admin-web.session-token");
    expect(sessionStorage.getItem(STORAGE_KEY)).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "salir" }));
    expect(screen.getByTestId("state")).toHaveTextContent("sin-sesion");
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("hasRole: mayúsculas y alias cuentan; sin sesión es falso", () => {
    sessionStorage.setItem(STORAGE_KEY, fakeJwt(SUB, ["Credit_Analyst", "auditor"]));
    render(
      <SessionProvider>
        <Probe roles={["credit_analyst"]} />
      </SessionProvider>,
    );
    expect(screen.getByTestId("probe")).toHaveTextContent("true");
    cleanup();

    sessionStorage.setItem(STORAGE_KEY, fakeJwt(SUB, ["analyst"]));
    render(
      <SessionProvider>
        <Probe roles={["CREDIT_ANALYST"]} />
      </SessionProvider>,
    );
    expect(screen.getByTestId("probe")).toHaveTextContent("true");
    cleanup();

    sessionStorage.clear();
    render(
      <SessionProvider>
        <Probe roles={["auditor"]} />
      </SessionProvider>,
    );
    expect(screen.getByTestId("probe")).toHaveTextContent("false");
  });
});
