import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Task } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { MyTasks } from "./my-tasks";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

// 10:00 en Lima del 11 de noviembre de 2026: hoy va de 05:00Z a 05:00Z del día siguiente.
const NOW = Date.parse("2026-11-11T15:00:00Z");
const BASE = "/api/v1/o/acme/tasks/";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [],
};
const task = (id: string, changes: Partial<Task> = {}) =>
  ({
    ...{ id, type: "CALL", title: `Tarea ${id}`, description: "", assigned_user_id: "u1" },
    ...{ contact: { id: null, display_name: null }, opportunity_id: null },
    ...{ due_at: "2026-11-11T19:00:00Z", is_all_day: false, reminder_at: null },
    ...{ status: "PENDING", priority: "MEDIUM", completed_at: null, completed_by_user_id: null },
    ...{ cancel_reason: null, source: "MANUAL", created_at: "x", updated_at: "x", version: 2 },
    ...changes,
  }) as Task;

type Reply = { status: number; body?: unknown };
// El servidor de la prueba: responde según la ruta y los filtros que pidió el navegador.
function serve(handler: (url: URL, init?: RequestInit) => Reply) {
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
const page = (...results: Task[]) => ({ status: 200, body: { results, next: null } });
const show = (...codes: string[]) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions: codes.map((code) => ({ code, scopes: [] })) }}>
      <MyTasks />
    </TenantProvider>,
  );
const gets = (calls: { url: URL; init?: RequestInit }[]) =>
  calls.filter(({ init }) => !init?.method || init.method === "GET").map(({ url }) => url);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Mis tareas (F5-08c)", () => {
  it("pide las de hoy con los límites del día en la zona de la pantalla", async () => {
    const calls = serve(() => page(task("1")));
    show("tasks.view");
    expect(await screen.findByText("Tarea 1")).toBeVisible();
    const [url] = gets(calls);
    expect(url!.pathname).toBe(BASE);
    expect(Object.fromEntries(url!.searchParams)).toEqual({
      mine: "true",
      due_from: "2026-11-11T05:00:00.000Z",
      due_to: "2026-11-12T05:00:00.000Z",
      limit: "100",
    });
    expect(screen.getByRole("heading", { level: 1, name: "Mis tareas" })).toBeVisible();
  });

  it("las pestañas piden Vencidas y Próximas, y se mueven con las flechas", async () => {
    const calls = serve((url) => {
      if (url.searchParams.get("overdue")) {
        return page(
          task("v1", { due_at: "2026-11-10T05:00:00Z", is_all_day: true }),
          task("hoy", { due_at: "2026-11-11T05:00:00Z", is_all_day: true }), // hoy: aún no vence
          task("v2", { due_at: "2026-11-11T14:00:00Z" }),
        );
      }
      return page(task("p1", { due_at: "2026-11-20T05:00:00Z", is_all_day: true }));
    });
    show("tasks.view");
    await screen.findByText("Tarea p1");
    const today = screen.getByRole("tab", { name: "Hoy" });
    today.focus();
    fireEvent.keyDown(today, { key: "ArrowRight" });
    expect(await screen.findByText("Tarea v1")).toBeVisible();
    expect(screen.getByRole("tab", { name: "Vencidas" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Vencidas" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Tarea v2")).toBeVisible();
    expect(screen.queryByText("Tarea hoy")).toBeNull();
    expect(screen.getAllByText(/Vencida$/).length).toBe(2);
    const overdue = gets(calls).find((url) => url.searchParams.get("overdue") === "true");
    expect(overdue?.searchParams.get("mine")).toBe("true");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Vencidas" }), { key: "ArrowRight" });
    expect(await screen.findByText("Tarea p1")).toBeVisible();
    const upcoming = gets(calls).find(
      (url) => url.searchParams.get("due_from") === "2026-11-12T05:00:00.000Z",
    );
    expect(upcoming?.searchParams.get("due_to")).toBeNull();
  });

  it("una pestaña abierta de un día para otro pide el día nuevo al volver a ella", async () => {
    const calls = serve(() => page(task("1")));
    show("tasks.view");
    await screen.findByText("Tarea 1");
    expect(gets(calls)[0]!.searchParams.get("due_from")).toBe("2026-11-11T05:00:00.000Z");
    vi.setSystemTime(Date.parse("2026-11-12T15:00:00Z")); // pasó la medianoche de Lima
    fireEvent.focus(window);
    await waitFor(() =>
      expect(gets(calls).at(-1)!.searchParams.get("due_from")).toBe("2026-11-12T05:00:00.000Z"),
    );
    expect(gets(calls).at(-1)!.searchParams.get("due_to")).toBe("2026-11-13T05:00:00.000Z");
  });

  it("Inicio, Fin y la flecha izquierda dan la vuelta entre pestañas", async () => {
    serve(() => page(task("1")));
    show("tasks.view");
    await screen.findByText("Tarea 1");
    const tabs = () => screen.getByRole("tablist");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Hoy" }), { key: "End" });
    expect(screen.getByRole("tab", { name: "Próximas" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tabs(), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Hoy" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tabs(), { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "Próximas" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(tabs(), { key: "Home" });
    expect(screen.getByRole("tab", { name: "Hoy" })).toHaveAttribute("aria-selected", "true");
    // Alt+flecha es «atrás» del navegador: ni se traga ni cambia de pestaña.
    expect(fireEvent.keyDown(tabs(), { key: "ArrowRight", altKey: true })).toBe(true);
    expect(screen.getByRole("tab", { name: "Hoy" })).toHaveAttribute("aria-selected", "true");
  });

  it("el texto de otra persona es texto: no se interpreta y va aislado", async () => {
    serve(() =>
      page(
        task("1", {
          title: "<img src=x onerror=alert(1)> ‮oculto",
          description: "línea 1\nlínea 2 <b>no</b>",
          contact: { id: "c1", display_name: "<i>Rosa</i>" },
        }),
      ),
    );
    show("tasks.view");
    const title = await screen.findByText(/<img src=x/);
    expect(title.tagName).toBe("BDI");
    expect(title).toHaveAttribute("dir", "auto");
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText(/línea 2 <b>no<\/b>/).tagName).toBe("BDI");
    const link = screen.getByRole("link", { name: /Cliente:/ });
    expect(link).toHaveAttribute("href", "/o/acme/clientes/c1");
    expect(within(link).getByText("<i>Rosa</i>").tagName).toBe("BDI");
  });

  it("un cliente sin nombre visible se dice sin enlace; las cerradas, atenuadas y al final", async () => {
    serve(() =>
      page(
        task("1", { contact: { id: "c9", display_name: null } }),
        task("2", { opportunity_id: "o7" }),
        task("3", { status: "DONE" }),
      ),
    );
    show("tasks.view");
    expect(await screen.findByText(/sin permiso para ver su nombre/)).toBeVisible();
    expect(screen.queryByRole("link", { name: /Cliente/ })).toBeNull();
    expect(screen.getByRole("link", { name: "Ver la oportunidad" })).toHaveAttribute(
      "href",
      "/o/acme/oportunidades/o7",
    );
    // las cerradas, atenuadas y con su estado, después de las abiertas
    expect(screen.getByText("Completada")).toBeVisible();
    expect(screen.getByText("Tarea 3").closest("li")).toHaveClass("opacity-75");
    expect(screen.getAllByRole("listitem")[2]).toHaveTextContent("Tarea 3");
  });

  it("vacío, carga, error con reintento y sin permiso", async () => {
    let reply: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    serve(() => reply);
    show("tasks.view");
    expect(await screen.findByText("Cargando tareas…")).toBeVisible();
    expect(await screen.findByRole("alert")).toBeVisible();
    reply = { status: 200, body: { results: [], next: null } };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("No tienes tareas para hoy.")).toBeVisible();
    cleanup();
    reply = { status: 403, body: { code: "PERMISSION_DENIED" } };
    show("tasks.view");
    expect(await screen.findByText(/No tienes permiso para ver las tareas/)).toBeVisible();
  });

  it("«Cargar más» suma la página siguiente y, si falla, deja lo cargado", async () => {
    let second: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    serve((url) =>
      url.searchParams.get("cursor")
        ? second
        : { status: 200, body: { results: [task("1")], next: `${BASE}?cursor=c2` } },
    );
    show("tasks.view");
    await screen.findByText("Tarea 1");
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.getByText("Tarea 1")).toBeVisible();
    second = { status: 403, body: { code: "PERMISSION_DENIED" } }; // una página siguiente
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText(/No tienes permiso para ver las tareas/)).toBeVisible();
    expect(screen.getByText("Tarea 1")).toBeVisible();
    second = page(task("2"));
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("Tarea 2")).toBeVisible();
    expect(screen.getByText("Tarea 1")).toBeVisible();
  });
});
