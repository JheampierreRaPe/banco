// Cliente HTTP del panel (E3-T10).
// - Base URL: `VITE_API_BASE_URL` (defecto same-origin `/api/v1` via proxy nginx).
// - Auth: `Authorization: Bearer <jwt>` de la sesión del analista
//   (sesión separada de la app móvil, docs/13#2.3).
// - Envoltorio `docs/05#4` (`{"data", "meta"}`) y errores problem+json
//   vía `AppError` (`code` + `status`).
// - Sin PII ni tokens en logs: los errores solo exponen `code`/`status`.

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

export function apiBaseUrl(): string {
  const fromEnv = import.meta.env.VITE_API_BASE_URL as string | undefined;
  return (fromEnv ?? "/api/v1").replace(/\/$/, "");
}

interface BackendError {
  code?: string;
  message?: string;
}

interface ProblemJson {
  code?: string;
  title?: string;
  detail?: string;
  message?: string;
  error?: BackendError;
}

function problemMessage(body: ProblemJson, fallback: string): string {
  return body.error?.message ?? body.detail ?? body.message ?? body.title ?? fallback;
}

export async function apiFetch<T>(
  path: string,
  token: string,
  init: RequestInit = {},
): Promise<{ data: T; meta: Record<string, unknown> }> {
  let res: Response;
  try {
    res = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        ...(init.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError("NETWORK_ERROR", "No se pudo conectar con el servidor. Reintenta.", 0);
  }

  if (!res.ok) {
    let code = "UNKNOWN_ERROR";
    let message = "Ocurrió un error. Reintenta.";
    try {
      const body = (await res.json()) as ProblemJson;
      const wrappedCode = body.error?.code;
      if (typeof wrappedCode === "string" && wrappedCode) code = wrappedCode;
      else if (typeof body.code === "string" && body.code) code = body.code;
      message = problemMessage(body, message);
    } catch {
      // Cuerpo no-JSON: se conserva el mensaje genérico (sin volcar nada).
    }
    throw new ApiError(code, message, res.status);
  }

  const envelope = (await res.json()) as { data: T; meta?: Record<string, unknown> };
  return { data: envelope.data, meta: envelope.meta ?? {} };
}

/** Mensaje accionable en español según el código de negocio (docs/05#6.4). */
export function friendlyErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case "NETWORK_ERROR":
        return "Sin conexión con el servidor. Revisa tu red y pulsa Reintentar.";
      case "NOT_AUTHENTICATED":
        return "Tu sesión expiró. Vuelve a ingresar.";
      case "NOT_AUTHORIZED":
        return "Tu usuario no tiene rol de analista de crédito.";
      case "NOT_FOUND":
        return "El expediente ya no existe.";
      case "CONFLICT":
      case "IDEMPOTENCY_CONFLICT":
        return "El expediente ya fue decidido por otro analista. Se actualizó la bandeja.";
      case "VALIDATION_ERROR":
        return err.message || "Revisa el motivo del dictamen e inténtalo de nuevo.";
      default:
        return err.message || "Ocurrió un error. Reintenta.";
    }
  }
  return "Ocurrió un error inesperado. Reintenta.";
}
