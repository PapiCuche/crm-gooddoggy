import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { vi } from "vitest";

import { shouldRetry } from "@/app/providers";

import messages from "../messages/es-PE.json";

export function renderIntl(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
      {ui}
    </NextIntlClientProvider>,
  );
}

// Con el cliente de API real: los tests sustituyen `fetch`, no los hooks generados.
export function renderApp(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: shouldRetry, retryDelay: 0 }, mutations: { retry: false } },
  });
  const view = renderIntl(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  return Object.assign(view, { client });
}

type Reply = { status: number; body?: unknown; headers?: Record<string, string> };

// Respuestas de la API por "MÉTODO ruta". Devuelve el doble para inspeccionar las llamadas.
export function mockApi(routes: Record<string, Reply | (() => Reply)>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const route = routes[`${(init?.method ?? "GET").toUpperCase()} ${String(input)}`];
    if (!route) return new Response(JSON.stringify({ code: "NOT_FOUND" }), { status: 404 });
    const { status, body, headers } = typeof route === "function" ? route() : route;
    return new Response(body === undefined ? null : JSON.stringify(body), { status, headers });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

// Envíos sin respuesta: caen cuando `expire()` vence su `AbortSignal.timeout` (`waits`, los plazos).
export function hangPosts(api: ReturnType<typeof mockApi>) {
  const [waits, timers] = [[] as number[], [] as AbortController[]];
  vi.spyOn(AbortSignal, "timeout").mockImplementation((ms) => {
    waits.push(ms);
    return timers[timers.push(new AbortController()) - 1]!.signal;
  });
  const inner = api.getMockImplementation()!;
  api.mockImplementation(async (input, init) => {
    if (init?.method !== "POST") return inner(input, init);
    const { signal } = init;
    return new Promise((_, fail) => signal?.addEventListener("abort", () => fail(signal.reason)));
  });
  const expire = () =>
    act(() => timers.forEach((t) => t.abort(new DOMException("", "TimeoutError"))));
  return { waits, expire, answer: () => api.mockImplementation(inner) };
}
