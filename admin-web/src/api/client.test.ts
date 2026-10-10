// Regresión DEFECTO 1: el backend responde {"error": {code, message, ...}}
// (backend/app/core/errors.py, docs/05#4), no la forma plana. apiFetch debe
// leer el envoltorio real con fallback a la forma plana.
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiFetch } from "./client";

function errorRes(body: unknown, status = 401): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiFetch parseo de errores del backend", () => {
  it("lee el envoltorio real {error:{code,message}} (NOT_AUTHENTICATED)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        errorRes(
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
      ),
    );
    try {
      await apiFetch("/credits/applications?status=IN_REVIEW", "jwt");
      expect.unreachable("debió lanzar ApiError");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      const apiErr = err as ApiError;
      expect(apiErr.code).toBe("NOT_AUTHENTICATED");
      expect(apiErr.message).toBe("Sesión expirada");
      expect(apiErr.status).toBe(401);
    }
  });

  it("mantiene fallback a la forma plana {code,message} (tests históricos)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => errorRes({ code: "CONFLICT", message: "ya decidido" }, 409)),
    );
    try {
      await apiFetch("/credits/applications/x/review", "jwt");
      expect.unreachable("debió lanzar ApiError");
    } catch (err) {
      const apiErr = err as ApiError;
      expect(apiErr.code).toBe("CONFLICT");
      expect(apiErr.message).toBe("ya decidido");
      expect(apiErr.status).toBe(409);
    }
  });

  it("el envoltorio real tiene prioridad sobre la forma plana", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        errorRes(
          {
            code: "PLANO",
            message: "plano",
            error: { code: "NOT_AUTHORIZED", message: "sin rol" },
          },
          403,
        ),
      ),
    );
    try {
      await apiFetch("/credits/applications?status=IN_REVIEW", "jwt");
      expect.unreachable("debió lanzar ApiError");
    } catch (err) {
      const apiErr = err as ApiError;
      expect(apiErr.code).toBe("NOT_AUTHORIZED");
      expect(apiErr.message).toBe("sin rol");
      expect(apiErr.status).toBe(403);
    }
  });
});
