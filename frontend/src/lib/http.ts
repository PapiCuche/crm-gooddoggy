// Única vía del cliente generado (orval) hacia la API (ADR-014): mismo origen, sesión en una
// cookie HttpOnly que el navegador envía solo, CSRF en los métodos no seguros y errores del
// contrato convertidos en `ApiError`. Nada de la sesión se guarda en el navegador. Solo para
// el navegador: usa una URL relativa y `document.cookie`.

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const CSRF_URL = "/api/v1/auth/csrf/";

export class ApiError extends Error {
  readonly status: number; // 0: la petición no llegó a tener respuesta
  readonly code: string;
  readonly fields?: Record<string, unknown>;
  readonly retryAfter?: number; // segundos enteros de `Retry-After`; la forma de fecha se ignora

  constructor(status: number, code: string, fields?: Record<string, unknown>, retryAfter?: number) {
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fields = fields;
    this.retryAfter = retryAfter;
  }
}

// orval tipa con esto el error de cada hook generado: lo que `apiFetch` lanza es un `ApiError`.
// eslint-disable-next-line @typescript-eslint/no-unused-vars
export type ErrorType<_Contract> = ApiError;

// La cookie tal cual: Django no la codifica, y lo que se compara son estos mismos caracteres.
function csrfToken(): string | null {
  return /(?:^|;\s*)csrftoken=([^;]+)/.exec(document.cookie)?.[1] ?? null;
}

// Sin respuesta: la red cayó, también a mitad del cuerpo. Una cancelación se relanza tal cual.
function unreachable(error: unknown): never {
  if ((error as { name?: unknown } | null)?.name === "AbortError") throw error;
  throw new ApiError(0, "NETWORK_ERROR");
}

export async function apiFetch<T>(url: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (UNSAFE.has(method)) {
    // El token y las cookies no salen de este origen, aunque alguien pase otra URL.
    if (new URL(url, document.baseURI).origin !== location.origin) {
      throw new Error("apiFetch: solo URLs del mismo origen");
    }
    // La cookie `csrftoken` es legible a propósito (ADR-003 §3): se copia en la cabecera. Si
    // pedirla falla, ese es el error que se ve; sin ella la escritura no puede pasar y no se envía.
    if (!csrfToken()) await apiFetch(CSRF_URL, { signal: options.signal });
    const token = csrfToken();
    if (!token) throw new ApiError(403, "CSRF_FAILED");
    headers.set("X-CSRFToken", token);
  }
  const init: RequestInit = { ...options, method, headers, credentials: "same-origin" };
  const response = await fetch(url, init).catch(unreachable);
  // Las descargas en CSV del contrato (una plantilla, un informe de errores) son un `Blob`.
  if (response.ok && /^text\/csv(;|$)/i.test(response.headers.get("Content-Type") ?? "")) {
    return (await response.blob().catch(unreachable)) as T;
  }
  const text = response.status === 204 ? "" : await response.text().catch(unreachable);
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    // La API solo sirve JSON (ADR-014): un 2xx con otro cuerpo no es una respuesta suya.
    if (response.ok) throw new ApiError(response.status, "INTERNAL_ERROR");
  }
  if (!response.ok) {
    const error = (body ?? {}) as { code?: unknown; fields?: unknown };
    const wait = response.headers.get("Retry-After") ?? "";
    throw new ApiError(
      response.status,
      typeof error.code === "string" ? error.code : "INTERNAL_ERROR",
      typeof error.fields === "object" && error.fields !== null && !Array.isArray(error.fields)
        ? (error.fields as Record<string, unknown>)
        : undefined,
      /^[1-9]\d{0,8}$/.test(wait) ? Number(wait) : undefined,
    );
  }
  return body as T;
}
