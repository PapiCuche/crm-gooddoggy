import { vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Task } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { MyTasks } from "./my-tasks";

// 10:00 en Lima del 11 de noviembre de 2026: hoy va de 05:00Z a 05:00Z del día siguiente.
export const NOW = Date.parse("2026-11-11T15:00:00Z");
export const BASE = "/api/v1/o/acme/tasks/";
export const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [],
};
export const task = (id: string, changes: Partial<Task> = {}) =>
  ({
    ...{ id, type: "CALL", title: `Tarea ${id}`, description: "", assigned_user_id: "u1" },
    ...{ contact: { id: null, display_name: null }, opportunity_id: null },
    ...{ due_at: "2026-11-11T19:00:00Z", is_all_day: false, reminder_at: null },
    ...{ status: "PENDING", priority: "MEDIUM", completed_at: null, completed_by_user_id: null },
    ...{ cancel_reason: null, source: "MANUAL", created_at: "x", updated_at: "x", version: 2 },
    ...changes,
  }) as Task;

export type Reply = { status: number; body?: unknown };
// El servidor de la prueba: responde según la ruta y los filtros que pidió el navegador.
export function serve(handler: (url: URL, init?: RequestInit) => Reply) {
  const calls: { url: URL; init?: RequestInit }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    calls.push({ url, init });
    const { status, body } = handler(url, init);
    return new Response(body === undefined ? null : JSON.stringify(body), { status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}
export const page = (...results: Task[]) => ({ status: 200, body: { results, next: null } });
export const show = (...codes: string[]) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions: codes.map((code) => ({ code, scopes: [] })) }}>
      <MyTasks />
    </TenantProvider>,
  );
export const gets = (calls: { url: URL; init?: RequestInit }[]) =>
  calls.filter(({ init }) => !init?.method || init.method === "GET").map(({ url }) => url);
