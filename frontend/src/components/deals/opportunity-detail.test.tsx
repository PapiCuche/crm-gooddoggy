import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { OpportunityDetail as Detail, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { OpportunityDetail } from "./opportunity-detail";

const ID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const BASE = `/api/v1/o/acme/opportunities/${ID}/`;
const DETAIL = `GET ${BASE}`;
const PATCH = `PATCH ${BASE}`;
const ASSIGN = `POST ${BASE}assign/`;
const MOVE = `POST ${BASE}move/`;
const PIPELINES = "GET /api/v1/o/acme/pipelines/?limit=200";
const MEMBERS = "GET /api/v1/o/acme/members/?limit=200";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("opportunities.view"),
};
const stage = (id: string, name: string, type: "OPEN" | "WON" | "LOST" = "OPEN") => ({
  ...{ id, name, type },
});
const detail = (changes: Partial<Detail> = {}) =>
  ({
    ...{ id: ID, number: "OPP-000007", title: "Collar para Rocky", pipeline_id: "p1" },
    ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, stage: stage("s1", "Nuevo") },
    ...{ status: "OPEN", amount: "1500.50", currency: "PEN", probability: 10, priority: "MEDIUM" },
    ...{
      assigned_user_id: "u1",
      next_action: "Llamar el viernes",
      expected_close_date: "2026-12-01",
    },
    ...{ stage_entered_at: "2026-10-30T10:00:00Z", last_activity_at: "2026-10-30T10:00:00Z" },
    ...{ won_at: null, lost_at: null, lost_reason: null, lost_reason_note: null, version: 3 },
    ...{ source: "MANUAL", next_action_at: null, created_at: "x", updated_at: "x" },
    ...{
      items: [{ variant_id: "v1", quantity: 2, estimated_unit_price: "750.25", notes: "Talla M" }],
      history: [
        { id: "h1", from_stage: null, to_stage: stage("s1", "Nuevo"), changed_by_user_id: "u1" },
        { id: "h2", from_stage: stage("s1", "Nuevo"), to_stage: stage("s2", "Cotizado") },
      ].map((row) => ({
        changed_at: "2026-10-30T10:00:00Z",
        seconds_in_previous_stage: null,
        ...row,
      })),
    },
    ...changes,
  }) as unknown as Detail;
const stages = ["s1", "s2", "s3"].map((id, i) => ({
  ...{ id, name: ["Nuevo", "Cotizado", "Ganado"][i], code: id, sort_order: i, is_active: true },
  ...{ stage_type: i === 2 ? "WON" : "OPEN", default_probability: 10, color: "#000" },
  ...{ stale_after_days: null, requires_fields: {} },
}));
const pipelines = {
  status: 200,
  body: {
    results: [
      {
        id: "p1",
        name: "Ventas",
        code: "p1",
        currency: "PEN",
        is_default: true,
        is_active: true,
        sort_order: 0,
        version: 1,
        kind: "RETAIL",
        stages,
      },
    ],
    next: null,
  },
};
const people = {
  status: 200,
  body: {
    results: [
      {
        id: "m2",
        status: "ACTIVE",
        user: { id: "u2", email: "l@acme.pe", first_name: "Luis", last_name: "Ramos" },
      },
    ],
    next: null,
  },
};
const ok = (body: unknown) => ({ status: 200, body });
const show = (codes: string[] = ["opportunities.view"], user = tenant.user) =>
  renderApp(
    <TenantProvider value={{ ...tenant, user, permissions: grants(...codes) }}>
      <OpportunityDetail opportunityId={ID} />
    </TenantProvider>,
  );
// `notFound()` lanza durante el render: React lo propaga como error de la ventana.
function notFoundThrown() {
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  return new Promise<unknown>((resolve) => {
    window.addEventListener("error", (event) => resolve(event.error), { once: true });
  }).finally(() => errors.mockRestore());
}
const sent = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls
    .filter(([, init]) => init?.method === method)
    .map(([, init]) => JSON.parse(String(init!.body)));
const reads = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.filter(([url, init]) => String(url) === BASE && (init?.method ?? "GET") === "GET")
    .length;
const value = (label: string) =>
  screen.getAllByRole("term").find((node) => node.textContent === label)!.nextElementSibling!;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-02T10:00:00Z"));
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("OpportunityDetail (F5-07)", () => {
  it("con quotes.view enseña las cotizaciones de esa oportunidad; sin él, no las pide", async () => {
    configure({ asyncUtilTimeout: 4000 });
    const QUOTES = `GET /api/v1/o/acme/quotes/?opportunity_id=${ID}&limit=50`;
    const row = {
      id: "q1",
      number: "COT-000042",
      status: "SENT",
      currency: "PEN",
      total: "500.00",
    };
    const api = mockApi({
      [DETAIL]: ok(detail()),
      [PIPELINES]: pipelines,
      [QUOTES]: ok({ results: [{ ...row, valid_until: null }], next: null }),
    });
    show();
    await screen.findByRole("heading", { level: 1, name: "Collar para Rocky" });
    expect(api.mock.calls.some(([url]) => String(url).includes("/quotes/"))).toBe(false);
    cleanup();
    show(["opportunities.view", "quotes.view"]);
    expect(await screen.findByText("COT-000042")).toBeVisible();
    expect(screen.getByRole("link", { name: "Ver todas las de este cliente" })).toHaveAttribute(
      "href",
      "/o/acme/cotizaciones?cliente=c1",
    );
  });

  it("con tasks.view enseña las tareas de esa oportunidad; sin él, no las pide", async () => {
    configure({ asyncUtilTimeout: 4000 }); // varios pasos seguidos: con la máquina cargada, más margen
    const TASKS = `GET /api/v1/o/acme/tasks/?opportunity_id=${ID}&limit=200`;
    const row = { id: "t1", type: "CALL", title: "Enviar la cotización", description: "" };
    const api = mockApi({
      [DETAIL]: ok(detail()),
      [PIPELINES]: pipelines,
      [TASKS]: ok({
        results: [{ ...row, due_at: null, is_all_day: false, status: "PENDING", priority: "HIGH" }],
        next: null,
      }),
    });
    show();
    await screen.findByRole("heading", { level: 1, name: "Collar para Rocky" });
    expect(api.mock.calls.some(([url]) => String(url).includes("/tasks/"))).toBe(false);
    cleanup();
    show(["opportunities.view", "tasks.view"]);
    expect(await screen.findByText("Enviar la cotización")).toBeVisible();
  });

  it("enseña el cliente con su ficha, el importe, la etapa, el responsable, las líneas y el historial", async () => {
    mockApi({ [DETAIL]: ok(detail()), [PIPELINES]: pipelines });
    show();
    expect(screen.getAllByRole("status").map((one) => one.textContent)).toContain(
      "Cargando la oportunidad…",
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: "Collar para Rocky" }),
    ).toBeVisible();
    expect(screen.getByText("OPP-000007")).toBeVisible();
    expect(
      within(value("Cliente") as HTMLElement).getByRole("link", { name: "Rosa Díaz" }),
    ).toHaveAttribute("href", "/o/acme/clientes/c1");
    expect(value("Importe")).toHaveTextContent("S/ 1,500.50");
    expect(value("Etapa")).toHaveTextContent("Nuevo · Abierta");
    expect(value("Probabilidad")).toHaveTextContent("10 %");
    expect(value("Responsable")).toHaveTextContent("Yo");
    expect(value("Próxima acción")).toHaveTextContent("Llamar el viernes");
    expect(value("Cierre esperado")).toHaveTextContent("2026-12-01");
    expect(value("En la etapa")).toHaveTextContent("3 días");
    const lines = within(screen.getByRole("region", { name: "Líneas" }));
    expect(lines.getByText("Línea 1: 2 × S/ 750.25")).toBeVisible();
    expect(lines.getByText("Talla M")).toBeVisible();
    const history = within(
      screen.getByRole("region", { name: "Historial de etapas" }),
    ).getAllByRole("listitem");
    expect(history[0]).toHaveTextContent("Creada en «Nuevo»");
    expect(history[1]).toHaveTextContent("De «Nuevo» a «Cotizado»");
    expect(screen.getByRole("link", { name: "Oportunidades" })).toHaveAttribute(
      "href",
      "/o/acme/oportunidades",
    );
  });

  it("un cliente que no se ve se dice y no tiene enlace; una perdida enseña su motivo y su nota", async () => {
    mockApi({
      [DETAIL]: ok(
        detail({
          ...{
            contact: { id: "c1", display_name: null },
            status: "LOST",
            stage: stage("s9", "Perdido", "LOST"),
          },
          ...{
            lost_reason: { id: "r1", name: "Precio" },
            lost_reason_note: "Compró en otra tienda",
          },
        }),
      ),
      [PIPELINES]: pipelines,
    });
    show();
    expect(await screen.findByText("Cliente no visible")).toBeVisible();
    expect(screen.queryByRole("link", { name: /Rosa/ })).toBeNull();
    expect(value("Motivo de pérdida")).toHaveTextContent("Precio");
    expect(value("Nota")).toHaveTextContent("Compró en otra tienda");
    expect(screen.getByRole("button", { name: /^Reabrir «Collar para Rocky»/ })).toBeVisible();
  });

  it("lo que escribió otra persona va aislado y una abierta no enseña campos de pérdida", async () => {
    const hostile = "x‮abc";
    mockApi({
      [DETAIL]: ok(
        detail({
          ...{ title: hostile, next_action: hostile, stage: stage("s1", hostile) },
          ...{ contact: { id: "c1", display_name: hostile } },
          items: [{ variant_id: "v1", quantity: 1, estimated_unit_price: "1", notes: hostile }],
          history: [
            {
              id: "h1",
              from_stage: stage("s0", hostile),
              to_stage: stage("s1", hostile),
              changed_by_user_id: null,
              changed_at: "2026-10-30T10:00:00Z",
              seconds_in_previous_stage: null,
            },
          ],
        }),
      ),
      [PIPELINES]: pipelines,
    });
    const { container } = show();
    await screen.findByText("OPP-000007"); // ya leyó el detalle
    // título, cliente, etapa, próxima acción, nota de línea y las dos etapas del historial: cada uno en su `<bdi>`
    const isolated = [...container.querySelectorAll("bdi")].filter(
      (one) => one.textContent === hostile,
    );
    expect(isolated).toHaveLength(7);
    expect(screen.queryByText("Motivo de pérdida")).toBeNull();
    expect(screen.queryByText("Nota")).toBeNull();
    expect(screen.getByRole("button", { name: /^Ganar «/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /^Perder «/ })).toBeVisible();
  });

  it("una oportunidad cerrada no se edita ni se asigna aunque haya permiso: se reabre", async () => {
    mockApi({
      [DETAIL]: ok(detail({ status: "WON", stage: stage("s3", "Ganado", "WON") })),
      [PIPELINES]: pipelines,
      [MEMBERS]: people,
    });
    show(["opportunities.view", "opportunities.update", "opportunities.assign", "users.view"]);
    expect(
      await screen.findByRole("button", { name: /^Reabrir «Collar para Rocky»/ }),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Editar" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Asignar a" })).toBeNull();
  });

  it("un 404 es la página de «no encontrada», sin decir si existe", async () => {
    mockApi({ [DETAIL]: { status: 404, body: { code: "NOT_FOUND" } }, [PIPELINES]: pipelines });
    const failure = notFoundThrown();
    show();
    expect(await failure).toMatchObject({ message: "NEXT_NOT_FOUND" });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("un 403 se explica sin reintento y un fallo se reintenta", async () => {
    mockApi({
      [DETAIL]: { status: 403, body: { code: "PERMISSION_DENIED" } },
      [PIPELINES]: pipelines,
    });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver esta oportunidad.",
    );
    expect(screen.queryByRole("button", { name: "Reintentar" })).toBeNull();
    cleanup();
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    mockApi({ [DETAIL]: () => reply, [PIPELINES]: pipelines });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    reply = ok(detail());
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Collar para Rocky" }),
    ).toBeVisible();
  });

  it("editar envía solo lo que cambió con la versión leída, enseña lo que devuelve la API y no pide nada si no cambió", async () => {
    const api = mockApi({
      [DETAIL]: ok(detail()),
      [PIPELINES]: pipelines,
      [PATCH]: ok(
        detail({ title: "Collar nuevo", amount: "1600.00", version: 4, next_action: null }),
      ),
    });
    show(["opportunities.view", "opportunities.update"]);
    fireEvent.click(await screen.findByRole("button", { name: "Editar" }));
    const form = await screen.findByRole("form", { name: "Editar la oportunidad" });
    // sin cambios: se cierra sin pedir nada
    fireEvent.click(within(form).getByRole("button", { name: "Guardar cambios" }));
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(sent(api, "PATCH")).toEqual([]);
    fireEvent.click(await screen.findByRole("button", { name: "Editar" }));
    const again = await screen.findByRole("form", { name: "Editar la oportunidad" });
    fireEvent.change(within(again).getByLabelText("Título"), { target: { value: "Collar nuevo" } });
    fireEvent.change(within(again).getByLabelText("Importe"), { target: { value: "1600.00" } });
    fireEvent.change(within(again).getByLabelText(/^Próxima acción/), { target: { value: "" } });
    fireEvent.click(within(again).getByRole("button", { name: "Guardar cambios" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Collar nuevo" })).toBeVisible();
    expect(sent(api, "PATCH")).toEqual([
      { title: "Collar nuevo", amount: "1600.00", next_action: null, version: 3 },
    ]);
    expect(await screen.findByText("Cambios guardados.")).toBeVisible();
    expect(value("Importe")).toHaveTextContent("S/ 1,600.00");
    await waitFor(() => expect(screen.getByRole("button", { name: "Editar" })).toHaveFocus());
    // otra edición empieza: el aviso de la anterior ya no aplica
    fireEvent.click(screen.getByRole("button", { name: "Editar" }));
    await screen.findByRole("form", { name: "Editar la oportunidad" });
    expect(screen.queryByText("Cambios guardados.")).toBeNull();
  });

  it("un 409 al editar dice que otra persona la cambió y vuelve a leer; el importe inválido no se envía", async () => {
    const api = mockApi({
      [DETAIL]: ok(detail()),
      [PIPELINES]: pipelines,
      [PATCH]: { status: 409, body: { code: "STALE_VERSION" } },
    });
    show(["opportunities.view", "opportunities.update"]);
    fireEvent.click(await screen.findByRole("button", { name: "Editar" }));
    const form = await screen.findByRole("form", { name: "Editar la oportunidad" });
    fireEvent.change(within(form).getByLabelText("Importe"), { target: { value: "abc" } });
    fireEvent.click(within(form).getByRole("button", { name: "Guardar cambios" }));
    expect(await within(form).findByText(/El importe no es válido/)).toBeVisible();
    expect(sent(api, "PATCH")).toEqual([]);
    fireEvent.change(within(form).getByLabelText("Importe"), { target: { value: "1700" } });
    fireEvent.click(within(form).getByRole("button", { name: "Guardar cambios" }));
    expect(
      await within(form).findByText(/Otra persona cambió esta oportunidad antes que tú/),
    ).toBeVisible();
    await waitFor(() => expect(reads(api)).toBe(2)); // se volvió a leer
  });

  it("sin `opportunities.update` ni `opportunities.assign` no hay controles de edición", async () => {
    mockApi({ [DETAIL]: ok(detail()), [PIPELINES]: pipelines, [MEMBERS]: people });
    show(["opportunities.view", "users.view"]);
    await screen.findByRole("heading", { level: 1, name: "Collar para Rocky" });
    expect(screen.queryByRole("button", { name: "Editar" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Asignar a" })).toBeNull();
  });

  it("asignar envía el responsable con la versión leída, explica el 400 y el 409", async () => {
    let reply: { status: number; body: unknown } = {
      status: 400,
      body: { code: "VALIDATION_ERROR" },
    };
    const api = mockApi({
      [DETAIL]: ok(detail()),
      [PIPELINES]: pipelines,
      [MEMBERS]: people,
      [ASSIGN]: () => reply,
    });
    show(["opportunities.view", "opportunities.assign", "users.view"]);
    const pick = await screen.findByRole("combobox", { name: "Asignar a" });
    await within(pick).findByRole("option", { name: "Luis Ramos" });
    fireEvent.click(screen.getByRole("button", { name: "Asignar" }));
    expect(await screen.findByText("Elige primero a quién asignarla.")).toBeVisible();
    fireEvent.change(pick, { target: { value: "u2" } });
    fireEvent.click(screen.getByRole("button", { name: "Asignar" }));
    expect(await screen.findByText("Esa persona no puede recibir la oportunidad.")).toBeVisible();
    expect(sent(api, "POST")).toEqual([{ assigned_user_id: "u2", version: 3 }]);
    reply = { status: 409, body: { code: "STALE_VERSION" } };
    fireEvent.click(screen.getByRole("button", { name: "Asignar" }));
    expect(
      await screen.findByText(/Otra persona cambió esta oportunidad antes que tú/),
    ).toBeVisible();
    await waitFor(() => expect(reads(api)).toBe(2));
    reply = ok(detail({ assigned_user_id: "u2", version: 5 }));
    fireEvent.click(screen.getByRole("button", { name: "Asignar" }));
    expect(await screen.findByText("Responsable cambiado.")).toBeVisible();
    expect(value("Responsable")).toHaveTextContent("Luis Ramos");
  });

  it("dos pulsaciones seguidas de «Asignar» envían una sola petición", async () => {
    const api = mockApi({
      [DETAIL]: ok(detail()),
      [PIPELINES]: pipelines,
      [MEMBERS]: people,
      [ASSIGN]: ok(detail({ assigned_user_id: "u2", version: 4 })),
    });
    show(["opportunities.view", "opportunities.assign", "users.view"]);
    const pick = await screen.findByRole("combobox", { name: "Asignar a" });
    await within(pick).findByRole("option", { name: "Luis Ramos" });
    fireEvent.change(pick, { target: { value: "u2" } });
    const button = screen.getByRole("button", { name: "Asignar" });
    fireEvent.click(button);
    fireEvent.click(button);
    await screen.findByText("Responsable cambiado.");
    expect(sent(api, "POST")).toHaveLength(1);
  });

  it("un 409 OPPORTUNITY_CLOSED al editar se dice aunque el formulario desaparezca", async () => {
    let closed = false;
    mockApi({
      [DETAIL]: () =>
        ok(detail(closed ? { status: "WON", stage: stage("s3", "Ganado", "WON") } : {})),
      [PIPELINES]: pipelines,
      [PATCH]: () => {
        closed = true;
        return { status: 409, body: { code: "OPPORTUNITY_CLOSED" } };
      },
    });
    show(["opportunities.view", "opportunities.update"]);
    fireEvent.click(await screen.findByRole("button", { name: "Editar" }));
    const form = await screen.findByRole("form", { name: "Editar la oportunidad" });
    fireEvent.change(within(form).getByLabelText("Título"), { target: { value: "Otro" } });
    fireEvent.click(within(form).getByRole("button", { name: "Guardar cambios" }));
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(await screen.findByRole("alert")).toHaveTextContent("Esta oportunidad ya está cerrada");
  });

  it("sin directorio solo se puede asignar a uno mismo, y un importe igual escrito de otra forma no se envía", async () => {
    const api = mockApi({ [DETAIL]: ok(detail()), [PIPELINES]: pipelines });
    show(["opportunities.view", "opportunities.assign", "opportunities.update"]);
    const pick = await screen.findByRole("combobox", { name: "Asignar a" });
    expect(
      within(pick)
        .getAllByRole("option")
        .map((one) => one.textContent),
    ).toEqual(["Asignar a…", "Yo"]);
    fireEvent.click(screen.getByRole("button", { name: "Editar" }));
    const form = await screen.findByRole("form", { name: "Editar la oportunidad" });
    fireEvent.change(within(form).getByLabelText("Importe"), { target: { value: "1500.5" } });
    fireEvent.click(within(form).getByRole("button", { name: "Guardar cambios" }));
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(sent(api, "PATCH")).toEqual([]);
  });

  it("mover la etapa usa la acción del tablero con la versión leída y vuelve a leer el detalle", async () => {
    let moved = false;
    const api = mockApi({
      [DETAIL]: () => ok(detail(moved ? { stage: stage("s2", "Cotizado"), version: 4 } : {})),
      [PIPELINES]: pipelines,
      [MOVE]: () => {
        moved = true;
        return ok(detail({ stage: stage("s2", "Cotizado"), version: 4 }));
      },
    });
    show();
    const select = await screen.findByRole("combobox", {
      name: "Mover «Collar para Rocky» a otra etapa",
    });
    await within(select).findByRole("option", { name: "Cotizado" });
    fireEvent.change(select, { target: { value: "s2" } });
    fireEvent.click(screen.getByRole("button", { name: "Mover «Collar para Rocky»" }));
    expect(await screen.findByText("«Collar para Rocky» pasó a «Cotizado».")).toBeVisible();
    await waitFor(() => expect(value("Etapa")).toHaveTextContent("Cotizado · Abierta"));
    expect(sent(api, "POST")).toEqual([{ stage_id: "s2", version: 3 }]);
    expect(reads(api)).toBe(2);
  });
});
