import { cleanup, configure, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Task } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { OverdueCount } from "./overdue-count";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "tasks.view", scopes: [] }],
};
const URL = "GET /api/v1/o/acme/tasks/?mine=true&overdue=true&limit=100";
const task = (due_at: string, is_all_day: boolean) =>
  ({ id: due_at, status: "PENDING", due_at, is_all_day }) as Task;
const show = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <OverdueCount />
    </TenantProvider>,
  );

beforeEach(() => vi.useFakeTimers({ toFake: ["Date"], now: Date.parse("2026-11-11T15:00:00Z") }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("contador de vencidas del menú", () => {
  it("cuenta las vencidas sin las de todo el día de hoy y lo dice también a los lectores", async () => {
    mockApi({
      [URL]: {
        status: 200,
        body: {
          results: [
            task("2026-11-10T05:00:00Z", true),
            task("2026-11-11T14:00:00Z", false),
            task("2026-11-11T05:00:00Z", true), // hoy: aún no vence
          ],
          next: null,
        },
      },
    });
    show();
    expect(await screen.findByText("2")).toBeVisible();
    expect(screen.getByText(/2 tareas vencidas/)).toBeInTheDocument();
  });

  it("con más de una página añade «+»; sin vencidas o con un fallo no pinta nada", async () => {
    mockApi({
      [URL]: { status: 200, body: { results: [task("2026-11-10T05:00:00Z", true)], next: "x" } },
    });
    show();
    expect(await screen.findByText("1+")).toBeVisible();
    cleanup();
    mockApi({ [URL]: { status: 200, body: { results: [], next: null } } });
    const none = show();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(none.container).toBeEmptyDOMElement();
    cleanup();
    mockApi({ [URL]: { status: 500, body: { code: "INTERNAL_ERROR" } } });
    const failed = show();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(failed.container).toBeEmptyDOMElement();
  });
});
