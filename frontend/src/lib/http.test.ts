import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiErrorKey } from "./api-errors";
import { ApiError, apiFetch } from "./http";

const TOKEN = "t".repeat(32); // con la forma de los de Django: 32 alfanuméricos
const fetchMock = vi.fn<typeof fetch>();
const json = (status: number, body: unknown, headers?: Record<string, string>) =>
  new Response(JSON.stringify(body), { status, headers });
const sent = (call = 0) => {
  const [url, init] = fetchMock.mock.calls[call] ?? [];
  return { url, init, headers: new Headers(init?.headers) };
};
const failure = (promise: Promise<unknown>) => promise.catch((caught: unknown) => caught);
// Una respuesta cuyo cuerpo se corta a medias.
const broken = (error: unknown) =>
  new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('{"a":'));
        controller.error(error);
      },
    }),
    { status: 200 },
  );

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  for (const name of ["csrftoken", "xcsrftoken"]) document.cookie = `${name}=; Max-Age=0`;
});
afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

describe("apiFetch", () => {
  it("lee sin CSRF, con las cookies del mismo origen, y devuelve el cuerpo", async () => {
    fetchMock.mockResolvedValue(json(200, { ok: true }));
    await expect(apiFetch("/api/v1/x/")).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sent().init?.credentials).toBe("same-origin");
    expect(sent().headers.get("Accept")).toBe("application/json");
    expect(sent().headers.has("X-CSRFToken")).toBe(false);
  });

  it("un nombre de cookie parecido no vale, y una lectura nunca lleva el token", async () => {
    document.cookie = "xcsrftoken=otro";
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(apiFetch("/api/v1/x/", { method: "POST" })).rejects.toMatchObject({
      code: "CSRF_FAILED",
    });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/v1/auth/csrf/"]); // la pide
    document.cookie = `csrftoken=${TOKEN}`;
    await apiFetch("https://otro.example/x"); // un GET, aunque fuera a otro origen
    expect(sent(1).headers.has("X-CSRFToken")).toBe(false);
  });

  it.each(["POST", "PUT", "PATCH", "DELETE"])("%s lleva el token de la cookie", async (method) => {
    document.cookie = `csrftoken=${TOKEN}`;
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    const { signal } = new AbortController();
    const headers = { "Content-Type": "application/json" };
    const written = apiFetch("/api/v1/x/", {
      method: method.toLowerCase(),
      headers,
      body: "{}",
      signal,
    });
    await expect(written).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sent().init).toMatchObject({ method, body: "{}", credentials: "same-origin" });
    expect(sent().init?.signal).toBe(signal);
    expect(sent().headers.get("X-CSRFToken")).toBe(TOKEN);
    expect(sent().headers.get("Content-Type")).toBe("application/json"); // lo que pasa quien llama
  });

  it("pide la cookie antes de la primera escritura si aún no existe", async () => {
    fetchMock.mockImplementationOnce(async () => {
      document.cookie = `csrftoken=${TOKEN}`;
      return new Response(null, { status: 204 });
    });
    fetchMock.mockResolvedValueOnce(json(200, {}));
    await apiFetch("/api/v1/x/", { method: "DELETE" });
    expect(sent(0).url).toBe("/api/v1/auth/csrf/");
    expect(sent(1).headers.get("X-CSRFToken")).toBe(TOKEN);
  });

  it("si no consigue la cookie no envía la escritura y dice por qué", async () => {
    fetchMock.mockResolvedValueOnce(json(429, { code: "RATE_LIMITED" }, { "Retry-After": "60" }));
    const limited = await failure(apiFetch("/api/v1/x/", { method: "POST", body: "{}" }));
    expect(limited).toMatchObject({ status: 429, code: "RATE_LIMITED", retryAfter: 60 });
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 })); // responde sin cookie
    const without = await failure(apiFetch("/api/v1/x/", { method: "POST", body: "{}" }));
    expect(without).toMatchObject({ status: 403, code: "CSRF_FAILED" });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/auth/csrf/",
      "/api/v1/auth/csrf/",
    ]);
  });

  it("no escribe en otro origen: el token no sale de este", async () => {
    document.cookie = `csrftoken=${TOKEN}`;
    for (const url of ["https://evil.example/x", "//evil.example/x", "/\\evil.example/x"]) {
      await expect(apiFetch(url, { method: "POST" })).rejects.toThrow("mismo origen");
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("convierte un error del contrato en ApiError con su código, campos y espera", async () => {
    const fields = { email: [{ code: "required" }] };
    fetchMock.mockResolvedValue(
      json(429, { code: "RATE_LIMITED", fields, message: "x" }, { "Retry-After": "30" }),
    );
    const error = await failure(apiFetch("/api/v1/x/"));
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 429, code: "RATE_LIMITED", fields, retryAfter: 30 });
    expect(apiErrorKey(error)).toBe("RATE_LIMITED");
    for (const wait of ["Wed, 21 Oct 2026 07:28:00 GMT", "0x1e", "0", "-5", "9".repeat(400)]) {
      fetchMock.mockResolvedValue(json(503, { code: 7, fields: ["x"] }, { "Retry-After": wait }));
      const odd = await failure(apiFetch("/api/v1/x/"));
      expect(odd).toMatchObject({
        code: "INTERNAL_ERROR",
        fields: undefined,
        retryAfter: undefined,
      });
    }
  });

  it("no interpreta un cuerpo que no es del contrato ni un código desconocido", async () => {
    fetchMock.mockResolvedValue(new Response("<h1>502</h1>", { status: 502 }));
    const html = await failure(apiFetch("/api/v1/x/"));
    expect(html).toMatchObject({ status: 502, code: "INTERNAL_ERROR", fields: undefined });
    fetchMock.mockResolvedValue(new Response("<h1>mantenimiento</h1>", { status: 200 }));
    const fake = await failure(apiFetch("/api/v1/x/")); // un 2xx que no es JSON no son datos
    expect(fake).toMatchObject({ status: 200, code: "INTERNAL_ERROR" });
    // Una descarga del contrato (`text/csv`) sí es un `Blob`; otro tipo o un error, no.
    const reply = (status: number, type: string) =>
      new Response("sku", { status, headers: { "Content-Type": type } });
    fetchMock.mockResolvedValue(reply(200, "text/csv; charset=utf-8"));
    await expect((await apiFetch<Blob>("/api/v1/x/")).text()).resolves.toBe("sku");
    for (const [status, type] of [
      [200, "text/csvx"],
      [200, "text/html"],
      [403, "text/csv"],
    ]) {
      fetchMock.mockResolvedValue(reply(status as number, type as string));
      expect(await failure(apiFetch("/api/v1/x/"))).toMatchObject({ code: "INTERNAL_ERROR" });
    }
    for (const code of ["ALGO_NUEVO", "constructor", "toString", "__proto__"]) {
      expect(apiErrorKey(new ApiError(409, code))).toBe("INTERNAL_ERROR");
    }
    expect(apiErrorKey(new TypeError("x"))).toBe("INTERNAL_ERROR");
  });

  it("distingue la red caída de una petición cancelada", async () => {
    const aborted = new DOMException("cancelada", "AbortError");
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const down = await failure(apiFetch("/api/v1/x/"));
    expect(down).toMatchObject({ status: 0, code: "NETWORK_ERROR" });
    expect(apiErrorKey(down)).toBe("NETWORK_ERROR");
    fetchMock.mockRejectedValueOnce(aborted);
    await expect(apiFetch("/api/v1/x/")).rejects.toBe(aborted);
    fetchMock.mockResolvedValueOnce(broken(new TypeError("network error"))); // a mitad del cuerpo
    expect(await failure(apiFetch("/api/v1/x/"))).toMatchObject({
      status: 0,
      code: "NETWORK_ERROR",
    });
    fetchMock.mockResolvedValueOnce(broken(aborted));
    await expect(apiFetch("/api/v1/x/")).rejects.toBe(aborted);
  });
});
