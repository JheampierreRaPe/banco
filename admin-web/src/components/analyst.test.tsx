// Pruebas de la vista de Analista (E3-T10) con API mockeada.
// Cubre: camino feliz (bandeja -> detalle -> APROBADO -> sale + auditado),
// motivo obligatorio, 409 ya decidido, 403 sin rol, enmascaramiento y los
// 4 estados de UI (contenido/carga/vacío/error).

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { SessionProvider } from "../auth/session";
import type { LoanApplication } from "../domain/types";

const APP_ID = "11111111-1111-4111-8111-111111111111";
const USER_ID = "22222222-2222-4222-8222-222222222222";
const PRODUCT_ID = "33333333-3333-4333-8333-333333333333";

function fakeJwt(roles: string[] = ["credit_analyst"]): string {
  const b64 = (o: unknown) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256" })}.${b64({ sub: USER_ID, roles })}.firma`;
}

function view(over: Partial<LoanApplication> = {}): LoanApplication {
  return {
    id: APP_ID,
    user_id: USER_ID,
    product_id: PRODUCT_ID,
    requested_amount_minor: 1_000_000,
    term_months: 12,
    status: "IN_REVIEW",
    bureau_authorized: true,
    bureau_authorized_at: "2026-10-08T10:24:00+00:00",
    score: 720,
    dti_ratio: "0.38",
    total_income_minor: 350000,
    total_debt_minor: 133000,
    decision_reason: "Derivado a revisión manual.",
    created_at: new Date(Date.now() - 12 * 60_000).toISOString(),
    updated_at: null,
    decided_at: null,
    ...over,
  };
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface MockRoutes {
  queue?: (url: string) => Response;
  review?: (url: string, body: unknown) => Response;
  products?: () => Response;
}

function mockApi(routes: MockRoutes) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = (init.method ?? "GET").toUpperCase();
    let body: unknown = null;
    try {
      body = init.body ? JSON.parse(init.body as string) : null;
    } catch {
      body = null;
    }
    calls.push({ url, method, body });
    if (url.includes("/credits/applications?status=IN_REVIEW")) {
      return routes.queue ? routes.queue(url) : json({ data: [], meta: { total: 0 } });
    }
    if (url.includes("/credits/products")) {
      return routes.products
        ? routes.products()
        : json({ data: [{ id: PRODUCT_ID, code: "personal", name: "Personal" }], meta: {} });
    }
    if (/\/credits\/applications\/.+\/review$/.test(url) && method === "POST") {
      if (routes.review) return routes.review(url, body);
      return json({ data: view({ status: "APPROVED", decided_at: new Date().toISOString() }), meta: {} });
    }
    return json({ code: "NOT_FOUND", message: "no mock" }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, calls };
}

function renderApp() {
  return render(
    <SessionProvider>
      <App />
    </SessionProvider>,
  );
}

async function signInAs(user: ReturnType<typeof userEvent.setup>, roles = ["credit_analyst"]) {
  const box = screen.getByLabelText("Token de sesión");
  await user.type(box, fakeJwt(roles));
  await user.click(screen.getByRole("button", { name: "Ingresar" }));
}

describe("vista de Analista de crédito", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("camino feliz: bandeja -> detalle -> APROBADO con motivo -> sale + auditado", async () => {
    const user = userEvent.setup();
    const decided = view({ status: "APPROVED", decided_at: "2026-10-09T12:00:00+00:00" });
    let queued = true;
    const { calls } = mockApi({
      queue: () =>
        queued
          ? json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } })
          : json({ data: [], meta: { total: 0, limit: 50, offset: 0 } }),
      review: () => {
        queued = false;
        return json({ data: decided, meta: {} });
      },
    });
    renderApp();
    await signInAs(user);

    // Bandeja con la fila enmascarada.
    const row = await screen.findByText(/Personal S\/ 10,000\.00/);
    expect(row).toBeInTheDocument();
    expect(screen.getByText(/Datos enmascarados\./)).toBeInTheDocument();

    // Detalle con riesgo del servidor + banner de consentimiento auditado.
    await user.click(row);
    expect(await screen.findByText(/Detalle CR-/)).toBeInTheDocument();
    expect(screen.getByText(/Score 720 - DTI 38% - Buró:/)).toBeInTheDocument();
    expect(screen.getByText(/Consentimiento buró: SÍ - 2026-10-08/)).toBeInTheDocument();

    // Dictamen con motivo obligatorio + confirmación explícita.
    await user.type(screen.getByLabelText("Motivo del dictamen"), "Verificado sustento de ingresos.");
    await user.click(screen.getByRole("button", { name: "Aprobar" }));

    // Confirmación: muestra decisión + referencia enmascarada, sin UUID crudo.
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.textContent).toMatch(/Confirmar dictamen: Aprobar CR-/);
    expect(dialog.textContent).not.toContain(APP_ID);
    await user.click(screen.getByRole("button", { name: "Sí, aprobar" }));

    // Sale de la bandeja y queda auditado (usuario, hora, antes/después).
    await waitFor(() =>
      expect(screen.getByText(/Sin expedientes por revisar/)).toBeInTheDocument(),
    );
    const audit = screen.getByRole("status");
    expect(audit.textContent).toMatch(/IN_REVIEW → APPROVED/);
    // La hora de la tira es la del servidor (`decided_at` del dictamen).
    expect(audit.textContent).toContain("2026-10-09T12:00:00+00:00");

    const posts = calls.filter((c) => c.method === "POST" && c.url.endsWith("/review"));
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toEqual({ decision: "APPROVED", reason: "Verificado sustento de ingresos." });
  });

  it("sin motivo no envía y muestra el aviso inline (sin confirmación)", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      queue: () => json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }),
    });
    renderApp();
    await signInAs(user);
    await user.click(await screen.findByText(/Personal S\/ 10,000\.00/));
    await user.click(await screen.findByRole("button", { name: "Aprobar" }));

    expect(await screen.findByText(/Sin motivo no se puede enviar/)).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("cancelar la confirmación no envía el dictamen", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      queue: () => json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }),
    });
    renderApp();
    await signInAs(user);
    await user.click(await screen.findByText(/Personal S\/ 10,000\.00/));
    await user.type(screen.getByLabelText("Motivo del dictamen"), "Revisión manual.");
    await user.click(screen.getByRole("button", { name: "Aprobar" }));

    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancelar" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);
    expect(screen.getByText(/Detalle CR-/)).toBeInTheDocument();
  });

  it("409 ya decidido: avisa y saca el expediente de la bandeja", async () => {
    const user = userEvent.setup();
    let queued = true;
    mockApi({
      queue: () =>
        queued
          ? json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } })
          : json({ data: [], meta: { total: 0, limit: 50, offset: 0 } }),
      review: () => {
        queued = false;
        return json({ code: "CONFLICT", message: "ya decidido" }, 409);
      },
    });
    renderApp();
    await signInAs(user);
    await user.click(await screen.findByText(/Personal S\/ 10,000\.00/));
    await user.type(screen.getByLabelText("Motivo del dictamen"), "Doble revisión.");
    await user.click(screen.getByRole("button", { name: "Rechazar" }));
    await user.click(await screen.findByRole("button", { name: "Sí, rechazar" }));

    expect(await screen.findByText(/ya fue decidido por otro analista/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/Sin expedientes por revisar/)).toBeInTheDocument(),
    );
  });

  it("403 en el dictamen: pantalla denegada (sin rol de analista)", async () => {
    const user = userEvent.setup();
    mockApi({
      queue: () => json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }),
      review: () => json({ code: "NOT_AUTHORIZED", message: "sin rol" }, 403),
    });
    renderApp();
    await signInAs(user);
    await user.click(await screen.findByText(/Personal S\/ 10,000\.00/));
    await user.type(screen.getByLabelText("Motivo del dictamen"), "Sin rol vigente.");
    await user.click(screen.getByRole("button", { name: "Aprobar" }));
    await user.click(await screen.findByRole("button", { name: "Sí, aprobar" }));

    expect(await screen.findByText("Acceso denegado")).toBeInTheDocument();
    expect(screen.getByText(/no tiene rol de analista de crédito/)).toBeInTheDocument();
  });

  it("422 del servidor: muestra el mensaje y el expediente sigue en bandeja", async () => {
    const user = userEvent.setup();
    mockApi({
      queue: () => json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }),
      review: () => json({ code: "VALIDATION_ERROR", message: "Motivo muy corto." }, 422),
    });
    renderApp();
    await signInAs(user);
    await user.click(await screen.findByText(/Personal S\/ 10,000\.00/));
    await user.type(screen.getByLabelText("Motivo del dictamen"), "x");
    await user.click(screen.getByRole("button", { name: "Rechazar" }));
    await user.click(await screen.findByRole("button", { name: "Sí, rechazar" }));

    expect(await screen.findByText(/Motivo muy corto\./)).toBeInTheDocument();
    // Sigue en bandeja: la fila y el detalle persisten.
    expect(screen.getByText(/Personal S\/ 10,000\.00/)).toBeInTheDocument();
    expect(screen.getByText(/Detalle CR-/)).toBeInTheDocument();
  });

  it("la bandeja pide limit/offset al servidor (paginación)", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      queue: () => json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }),
    });
    renderApp();
    await signInAs(user);
    await screen.findByText(/Personal S\/ 10,000\.00/);

    const queued = calls.filter((c) => c.url.includes("status=IN_REVIEW"));
    expect(queued.length).toBeGreaterThan(0);
    expect(queued[0].url).toContain("limit=");
    expect(queued[0].url).toContain("offset=");
    expect(queued[0].url).toContain("status=IN_REVIEW");
  });
  it("403 sin rol: pantalla denegada (bandeja y detalle bloqueados)", async () => {
    const user = userEvent.setup();
    mockApi({
      queue: () => json({ code: "NOT_AUTHORIZED", message: "sin rol" }, 403),
    });
    renderApp();
    await signInAs(user);
    expect(await screen.findByText("Acceso denegado")).toBeInTheDocument();
    expect(screen.getByText(/no tiene rol de analista de crédito/)).toBeInTheDocument();
  });

  it("sin rol de crédito en el token: va a su módulo sin llamar a la API", async () => {
    const user = userEvent.setup();
    const { fetchMock } = mockApi({});
    renderApp();
    await signInAs(user, ["auditor"]);
    // Corte 2 (Opción A): el auditor entra a `/audit` (placeholder F-T18);
    // la bandeja de crédito ni se consulta.
    expect(await screen.findByText(/llega en F-T18/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("nunca muestra UUID/documento/cuenta en claro", async () => {
    const user = userEvent.setup();
    mockApi({
      queue: () => json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }),
    });
    const { container } = renderApp();
    await signInAs(user);
    await user.click(await screen.findByText(/Personal S\/ 10,000\.00/));
    await screen.findByText(/Detalle CR-/);

    const text = container.textContent ?? "";
    expect(text).not.toContain(APP_ID);
    expect(text).not.toContain(USER_ID);
    expect(text).not.toContain(PRODUCT_ID);
    expect(text).toContain("••••");
  });

  it("estado vacío: sin expedientes", async () => {
    const user = userEvent.setup();
    mockApi({ queue: () => json({ data: [], meta: { total: 0, limit: 50, offset: 0 } }) });
    renderApp();
    await signInAs(user);
    expect(await screen.findByText(/Sin expedientes por revisar/)).toBeInTheDocument();
  });

  it("estado de error con Reintentar recupera la bandeja", async () => {
    const user = userEvent.setup();
    let fails = true;
    mockApi({
      queue: () => {
        if (fails) return json({ code: "UNKNOWN_ERROR", message: "caído" }, 500);
        return json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } });
      },
    });
    renderApp();
    await signInAs(user);
    expect(
      await screen.findByText(/No se pudo cargar la bandeja/, undefined, { timeout: 5000 }),
    ).toBeInTheDocument();

    fails = false;
    await user.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(
      await screen.findByText(/Personal S\/ 10,000\.00/, undefined, { timeout: 5000 }),
    ).toBeInTheDocument();
  });

  it("envoltorio real: 401 NOT_AUTHENTICATED muestra sesión expirada", async () => {
    const user = userEvent.setup();
    mockApi({
      queue: () =>
        json(
          {
            error: {
              code: "NOT_AUTHENTICATED",
              message: "Sesión expirada",
              details: {},
              request_id: "req-1",
            },
          },
          401,
        ),
    });
    renderApp();
    await signInAs(user);
    expect(
      await screen.findByText("Tu sesión expiró. Vuelve a ingresar."),
    ).toBeInTheDocument();
  });

  it("estado de carga: skeleton mientras llega la bandeja", async () => {
    const user = userEvent.setup();
    let release!: (v: Response) => void;
    const gate = new Promise<Response>((res) => {
      release = res;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).includes("/credits/products")) {
          return json({ data: [{ id: PRODUCT_ID, code: "personal", name: "Personal" }], meta: {} });
        }
        return gate;
      }),
    );
    renderApp();
    const box = screen.getByLabelText("Token de sesión");
    await user.type(box, fakeJwt());
    await user.click(screen.getByRole("button", { name: "Ingresar" }));
    expect(await screen.findByLabelText("Cargando bandeja")).toBeInTheDocument();
    release(json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }));
    expect(await screen.findByText(/Personal S\/ 10,000\.00/)).toBeInTheDocument();
  });

  it("401 NOT_AUTHENTICATED: 'Volver a ingresar' sale al login y limpia la sesión", async () => {
    const user = userEvent.setup();
    mockApi({
      queue: () =>
        json(
          {
            error: {
              code: "NOT_AUTHENTICATED",
              message: "Sesión expirada",
              details: {},
              request_id: "req-1",
            },
          },
          401,
        ),
    });
    renderApp();
    await signInAs(user);
    await screen.findByText("Tu sesión expiró. Vuelve a ingresar.");
    expect(sessionStorage.getItem("admin-web.session-token")).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "Volver a ingresar" }));

    expect(await screen.findByLabelText("Token de sesión")).toBeInTheDocument();
    expect(sessionStorage.getItem("admin-web.session-token")).toBeNull();
    expect(screen.queryByText("Tu sesión expiró. Vuelve a ingresar.")).not.toBeInTheDocument();
  });

  it("cerrar sesión desde el panel vuelve al login y limpia la sesión", async () => {
    const user = userEvent.setup();
    mockApi({
      queue: () => json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }),
    });
    renderApp();
    await signInAs(user);
    await screen.findByText(/Personal S\/ 10,000\.00/);

    await user.click(screen.getByRole("button", { name: "Cerrar sesión" }));

    expect(await screen.findByLabelText("Token de sesión")).toBeInTheDocument();
    expect(sessionStorage.getItem("admin-web.session-token")).toBeNull();
  });

  it("botón Refrescar vuelve a consultar la bandeja sin recargar la página", async () => {
    const user = userEvent.setup();
    const { calls } = mockApi({
      queue: () => json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }),
    });
    renderApp();
    await signInAs(user);
    await screen.findByText(/Personal S\/ 10,000\.00/);

    const before = calls.filter((c) => c.url.includes("status=IN_REVIEW")).length;
    await user.click(screen.getByRole("button", { name: "Refrescar" }));

    await waitFor(() =>
      expect(calls.filter((c) => c.url.includes("status=IN_REVIEW")).length).toBeGreaterThan(before),
    );
  });

  it("menú condicionado por rol: el analista solo ve Créditos (corte 2)", async () => {
    const user = userEvent.setup();
    mockApi({
      queue: () => json({ data: [view()], meta: { total: 1, limit: 50, offset: 0 } }),
    });
    renderApp();
    await signInAs(user);
    await screen.findByText(/Personal S\/ 10,000\.00/);

    // Corte 2: el menú muestra solo la unión de rutas del JWT; el analista
    // ve únicamente "Créditos" (activa) y nada deshabilitado.
    const nav = screen.getByLabelText("Navegación del panel");
    expect(within(nav).getByRole("button", { name: "Créditos" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(within(nav).queryByTitle("Próximamente")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cerrar sesión" })).toBeInTheDocument();
  });

  it("bandeja acotada: contador '50+' y 'Cargar más' trae el resto (paginación por offset)", async () => {
    const user = userEvent.setup();
    const all = Array.from({ length: 60 }, (_, i) =>
      view({
        id: `app-${String(i).padStart(4, "0")}`,
        requested_amount_minor: (i + 1) * 1000,
      }),
    );
    mockApi({
      queue: (url) => {
        const parsed = new URL(url, "http://localhost");
        const limit = Number(parsed.searchParams.get("limit"));
        const offset = Number(parsed.searchParams.get("offset"));
        const page = all.slice(offset, offset + limit);
        return json({ data: page, meta: { total: page.length, limit, offset } });
      },
    });
    renderApp();
    await signInAs(user);

    // Página llena (50) y puede haber más -> contador "50+" y botón Cargar más.
    await screen.findByText(/S\/ 10\.00/);
    expect(screen.getByText(/Bandeja IN_REVIEW \(50\+\)/)).toBeInTheDocument();
    expect(screen.queryByText(/S\/ 510\.00/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Cargar más/ }));

    expect(await screen.findByText(/S\/ 510\.00/)).toBeInTheDocument();
    expect(screen.getByText(/Bandeja IN_REVIEW \(60\)/)).toBeInTheDocument();
  });
});
