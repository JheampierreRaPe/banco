// Pruebas del shell + RBAC base (F-T12 corte 1).
// Cubre: sidebar/topbar/contenido, selector de rol visible, colapso de la
// navegación, `RoleGuard` (permitido/403) y páginas mínimas 403/404.

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SessionProvider } from "../auth/session";
import { AppShell } from "./AppShell";
import { RoleGuard } from "./RoleGuard";
import { ForbiddenPage, NotFoundPage } from "./pages";

const SUB = "22222222-2222-4222-8222-222222222222";

function fakeJwt(roles: unknown = ["credit_analyst"]): string {
  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256" })}.${b64({ sub: SUB, roles })}.firma`;
}

function shell(roles: unknown = ["credit_analyst"]) {
  sessionStorage.setItem(
    "admin-web.session-token",
    fakeJwt(roles),
  );
  return render(
    <SessionProvider>
      <AppShell activePath="/credits">
        <p>contenido</p>
      </AppShell>
    </SessionProvider>,
  );
}

describe("AppShell", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    sessionStorage.clear();
  });

  it("renderiza topbar, sidebar por rol y contenido", () => {
    shell();
    expect(screen.getByText("Panel interno — Banca Online")).toBeInTheDocument();
    expect(screen.getByLabelText("Navegación del panel")).toBeInTheDocument();
    expect(screen.getByText("contenido")).toBeInTheDocument();
    // Entrada activa de créditos + resto próximamente.
    expect(screen.getByRole("button", { name: "Créditos" })).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByTitle("Próximamente")).toHaveLength(5);
  });

  it("selector de rol visible: chip con un rol, select con varios", () => {
    shell();
    expect(screen.getByText("Rol: Créditos")).toBeInTheDocument();
    expect(screen.queryByLabelText("Rol activo")).not.toBeInTheDocument();
    cleanup();

    shell(["credit_analyst", "auditor"]);
    const select = screen.getByLabelText("Rol activo");
    expect(select).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Créditos" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Auditoría" })).toBeInTheDocument();
  });

  it("la navegación se colapsa con el botón (pantallas pequeñas)", async () => {
    const user = userEvent.setup();
    shell();
    const toggle = screen.getByRole("button", { name: "Ocultar navegación" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Mostrar navegación" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("cerrar sesión limpia el token del panel", async () => {
    const user = userEvent.setup();
    shell();
    await user.click(screen.getByRole("button", { name: "Cerrar sesión" }));
    expect(sessionStorage.getItem("admin-web.session-token")).toBeNull();
    expect(screen.queryByText("Rol: Créditos")).not.toBeInTheDocument();
  });
});

describe("RoleGuard y páginas", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    sessionStorage.clear();
  });

  function guard(roles: unknown, allow: string[]) {
    sessionStorage.setItem("admin-web.session-token", fakeJwt(roles));
    return render(
      <SessionProvider>
        <RoleGuard allow={allow}>
          <p>zona restringida</p>
        </RoleGuard>
      </SessionProvider>,
    );
  }

  it("rol permitido: muestra el contenido", () => {
    guard(["auditor"], ["auditor"]);
    expect(screen.getByText("zona restringida")).toBeInTheDocument();
  });

  it("rol incorrecto: 403 sin exponer el contenido", () => {
    guard(["auditor"], ["security_admin"]);
    expect(screen.getByText("Acceso denegado (403)")).toBeInTheDocument();
    expect(screen.queryByText("zona restringida")).not.toBeInTheDocument();
  });

  it("sin sesión: 403 (la puerta de sesión se cablea en el corte 2)", () => {
    render(
      <SessionProvider>
        <RoleGuard allow={["auditor"]}>
          <p>zona restringida</p>
        </RoleGuard>
      </SessionProvider>,
    );
    expect(screen.getByText("Acceso denegado (403)")).toBeInTheDocument();
  });

  it("NotFoundPage mínima", () => {
    render(<NotFoundPage />);
    expect(screen.getByText("Página no encontrada (404)")).toBeInTheDocument();
  });

  it("ForbiddenPage acepta pista sin PII", () => {
    render(<ForbiddenPage hint="Pista accionable." />);
    expect(screen.getByText("Pista accionable.")).toBeInTheDocument();
  });
});
