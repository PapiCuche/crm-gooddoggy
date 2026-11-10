import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
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
const MOVE = `POST ${BASE}move/`;
const PIPELINES = "GET /api/v1/o/acme/pipelines/?limit=200";
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
    show(["opportunities.view", "opportunities.update", "opportunities.assign"]);
    expect(await screen.findByText("Cliente no visible")).toBeVisible();
    expect(screen.queryByRole("link", { name: /Rosa/ })).toBeNull();
    expect(value("Motivo de pérdida")).toHaveTextContent("Precio");
    expect(value("Nota")).toHaveTextContent("Compró en otra tienda");
    expect(screen.queryByRole("button", { name: "Editar" })).toBeNull(); // cerrada: no se edita, se reabre
    expect(screen.getByRole("button", { name: /^Reabrir «Collar para Rocky»/ })).toBeVisible();
  });

  it("lo que escribió otra persona va aislado y una abierta no enseña campos de pérdida", async () => {
    const hostile = "x‮abc";
    mockApi({
      [DETAIL]: ok(
        detail({
          ...{ title: hostile, next_action: hostile },
          ...{ contact: { id: "c1", display_name: hostile } },
          items: [{ variant_id: "v1", quantity: 1, estimated_unit_price: "1", notes: hostile }],
        }),
      ),
      [PIPELINES]: pipelines,
    });
    const { container } = show();
    await screen.findByText("OPP-000007"); // ya leyó el detalle
    // título, cliente, próxima acción y nota de línea: cada uno en su `<bdi>`
    const isolated = [...container.querySelectorAll("bdi")].filter(
      (one) => one.textContent === hostile,
    );
    expect(isolated).toHaveLength(4);
    expect(screen.queryByText("Motivo de pérdida")).toBeNull();
    expect(screen.queryByText("Nota")).toBeNull();
    expect(screen.getByRole("button", { name: /^Ganar «/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /^Perder «/ })).toBeVisible();
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
