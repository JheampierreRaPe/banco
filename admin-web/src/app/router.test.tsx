// Pruebas del router del panel (F-T12 corte 2).
// Cubre: cada rol ve su ruta, unión multi-rol, `/` a la primera permitida,
// 403 por ruta, 404, sin token -> sesión, sin rol del panel -> 403 y
// 401 -> volver a sesión. Regresión de crédito: `analyst.test.tsx`.

import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { SessionProvider } from "../auth/session";

const SUB = "22222222-2222-4222-8222-222222222222";

function fakeJwt(roles: unknown): string {
  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256" })}.${b64({ sub: SUB, roles })}.firma`;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderApp(initialPath: string, roles: unknown | null) {
  sessionStorage.clear();
  if (roles !== null) sessionStorage.setItem("admin-web.session-token", fakeJwt(roles));
  return render(
    <SessionProvider>
      <App initialPath={initialPath} />
    </SessionProvider>,
  );
}

const PRODUCT_ID = "33333333-3333-4333-8333-333333333333";

function mockQueueEmpty() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (String(url).includes("/credits/products")) {
        return json({ data: [{ id: PRODUCT_ID, code: "personal", name: "Personal" }], meta: {} });
      }
      if (String(url).includes("/credits/applications?status=IN_REVIEW")) {
        return json({ data: [], meta: { total: 0, limit: 50, offset: 0 } });
      }
      return json({ code: "NOT_FOUND", message: "no mock" }, 404);
    }),
  );
}

describe("router del panel", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it.each([
    ["operations_analyst", "/operations", "Operaciones", "F-T13"],
    ["fraud_analyst", "/fraud", "Fraude", "F-T14"],
    ["compliance_officer", "/compliance", "Cumplimiento", "F-T15"],
    ["security_admin", "/admin", "Administración", "F-T17"],
    ["auditor", "/audit", "Auditoría", "F-T18"],
  ])("rol %s ve su ruta %s (placeholder %s)", async (role, path, title, task) => {
    renderApp(path, [role]);
    expect(await screen.findByRole("heading", { name: title })).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`llega en ${task}`))).toBeInTheDocument();
  });

  it("vista de crédito intacta bajo el router (/credits)", async () => {
    mockQueueEmpty();
    renderApp("/credits", ["credit_analyst"]);
    expect(await screen.findByText("Sin expedientes por revisar")).toBeInTheDocument();
  });

  it("`/` redirige a la primera ruta permitida de la unión", async () => {
    renderApp("/", ["fraud_analyst"]);
    expect(await screen.findByRole("heading", { name: "Fraude" })).toBeInTheDocument();
    cleanup();

    mockQueueEmpty();
    renderApp("/", ["auditor", "credit_analyst"]);
    // Unión ordenada por menú: créditos antes que auditoría.
    expect(await screen.findByText("Sin expedientes por revisar")).toBeInTheDocument();
  });

  it("unión multi-rol: el menú muestra ambas rutas", async () => {
    renderApp("/", ["credit_analyst", "auditor"]);
    const nav = await screen.findByLabelText("Navegación del panel");
    expect(within(nav).getByRole("button", { name: "Créditos" })).toBeInTheDocument();
    expect(within(nav).getByRole("button", { name: /Auditoría/ })).toBeInTheDocument();
  });

  it("rol incorrecto en una ruta existente -> 403 (sin exponer contenido)", async () => {
    renderApp("/credits", ["auditor"]);
    expect(await screen.findByText("Acceso denegado (403)")).toBeInTheDocument();
    expect(screen.getByText(/no tiene rol para Créditos/)).toBeInTheDocument();
    expect(screen.queryByText("Sin expedientes por revisar")).not.toBeInTheDocument();
    cleanup();

    renderApp("/audit", ["credit_analyst"]);
    expect(await screen.findByText("Acceso denegado (403)")).toBeInTheDocument();
    expect(screen.queryByText(/llega en F-T18/)).not.toBeInTheDocument();
  });

  it("ruta inexistente -> 404", async () => {
    renderApp("/no-existe", ["auditor"]);
    expect(await screen.findByText("Página no encontrada (404)")).toBeInTheDocument();
  });

  it("sin token -> sesión (puerta de login)", async () => {
    renderApp("/audit", null);
    expect(await screen.findByLabelText("Token de sesión")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ingresar" })).toBeInTheDocument();
  });

  it("usuario sin rol del panel -> 403 (no entra)", async () => {
    renderApp("/", ["otro_rol"]);
    expect(await screen.findByText("Acceso denegado (403)")).toBeInTheDocument();
    expect(screen.getByText(/no tiene rol del panel/)).toBeInTheDocument();
  });

  it("selector de rol: cambia la vista inicial sin restringir la unión", async () => {
    const user = userEvent.setup();
    mockQueueEmpty();
    renderApp("/", ["credit_analyst", "auditor"]);
    await screen.findByText("Sin expedientes por revisar");

    await user.selectOptions(screen.getByLabelText("Rol activo"), "auditor");
    expect(await screen.findByText(/llega en F-T18/)).toBeInTheDocument();
    // La unión sigue intacta en el menú.
    const nav = screen.getByLabelText("Navegación del panel");
    expect(within(nav).getByRole("button", { name: "Créditos" })).toBeInTheDocument();
  });

  it("401 en la bandeja -> volver a sesión (limpia el token)", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).includes("/credits/products")) {
          return json({ data: [], meta: {} });
        }
        return json(
          { error: { code: "NOT_AUTHENTICATED", message: "expirada", details: {}, request_id: "r" } },
          401,
        );
      }),
    );
    renderApp("/credits", ["credit_analyst"]);
    await screen.findByText("Tu sesión expiró. Vuelve a ingresar.");
    await user.click(screen.getByRole("button", { name: "Volver a ingresar" }));
    expect(await screen.findByLabelText("Token de sesión")).toBeInTheDocument();
    expect(sessionStorage.getItem("admin-web.session-token")).toBeNull();
  });

  it("navegar por el menú cambia de vista (ida y vuelta)", async () => {
    const user = userEvent.setup();
    mockQueueEmpty();
    renderApp("/audit", ["credit_analyst", "auditor"]);
    expect(await screen.findByText(/llega en F-T18/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Créditos" }));
    expect(await screen.findByText("Sin expedientes por revisar")).toBeInTheDocument();
  });
});
