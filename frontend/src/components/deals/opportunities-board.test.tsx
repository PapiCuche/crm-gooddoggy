import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Opportunity, Pipeline, SelfContext, Stage } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { OpportunitiesBoard } from "./opportunities-board";

const PIPELINES = "GET /api/v1/o/acme/pipelines/?limit=200";
const MEMBERS = "GET /api/v1/o/acme/members/?limit=200";
const LIST = "GET /api/v1/o/acme/opportunities/?pipeline_id=p1&status=OPEN";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("opportunities.view"),
};
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const stage = (id: string, name: string, order: number, changes: Partial<Stage> = {}): Stage => ({
  ...{ id, name, code: id, sort_order: order, stage_type: "OPEN", default_probability: 10 },
  ...{ stale_after_days: null, color: "#0ea5e9", requires_fields: {}, is_active: true },
  ...changes,
});
const pipeline = (id: string, name: string, changes: Partial<Pipeline> = {}): Pipeline => ({
  ...{ id, name, code: id, kind: "RETAIL", currency: "PEN", is_default: false, is_active: true },
  ...{ sort_order: 0, version: 1, stages: [] },
  ...changes,
});
const stages = [
  stage("s1", "Nuevo", 0),
  stage("s2", "Cotizado", 1),
  stage("s3", "Antigua", 2, { is_active: false }),
  stage("s4", "Ganado", 3, { stage_type: "WON" }),
];
const sales = pipeline("p1", "Ventas", { is_default: true, stages });
const opportunity = (id: string, changes: Partial<Opportunity> = {}): Opportunity => ({
  ...{ id, number: `OPP-00000${id}`, title: "Collar para Rocky", pipeline_id: "p1" },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" } },
  ...{ stage: { id: "s1", name: "Nuevo", type: "OPEN" }, status: "OPEN" },
  ...{ amount: "1500.50", currency: "PEN", probability: 10, priority: "MEDIUM" },
  ...{ assigned_user_id: "u1", next_action: "Llamar el viernes", expected_close_date: null },
  ...{ stage_entered_at: "2026-10-30T10:00:00Z", last_activity_at: "2026-10-30T10:00:00Z" },
  ...{ won_at: null, lost_at: null, lost_reason: null, version: 1 },
  ...changes,
});
const rocky = opportunity("1");
const luna = opportunity("2", {
  ...{ title: "Cama Luna", amount: "300.25", assigned_user_id: "u2", next_action: null },
  ...{ contact: { id: "c2", display_name: null } },
  ...{ stage: { id: "s2", name: "Cotizado", type: "OPEN" } },
  ...{ stage_entered_at: "2026-10-20T10:00:00Z" },
});
const mimi = opportunity("3", { title: "Correa Mimi", amount: "100.10", stage: luna.stage });

const show = (permissions = tenant.permissions) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <OpportunitiesBoard />
    </TenantProvider>,
  );
const column = (name: string) =>
  within(screen.getByRole("region", { name: "Tablero de oportunidades" }))
    .getByRole("heading", { name })
    .closest("section")!;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-02T10:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("OpportunitiesBoard", () => {
  it("una columna por etapa activa con su recuento y su total, y las tarjetas con sus datos", async () => {
    const api = mockApi({ [PIPELINES]: page([sales]), [LIST]: page([rocky, luna, mimi]) });
    show();
    const board = await screen.findByRole("region", { name: "Tablero de oportunidades" });
    // Nuevo, Cotizado; Ganado no (el estado por defecto es «Abiertas»); Antigua está inactiva y vacía.
    expect(
      within(board)
        .getAllByRole("heading", { level: 2 })
        .map((h) => h.textContent),
    ).toEqual(["Nuevo", "Cotizado"]);
    const nuevo = column("Nuevo");
    expect(nuevo).toHaveTextContent("1 oportunidad · S/ 1,500.50");
    expect(column("Cotizado")).toHaveTextContent("2 oportunidades · S/ 400.35");
    const [card] = within(nuevo).getAllByRole("listitem");
    expect(card).toHaveTextContent("Collar para Rocky");
    expect(card).toHaveTextContent("OPP-000001");
    expect(card).toHaveTextContent("Rosa Díaz");
    expect(card).toHaveTextContent("S/ 1,500.50");
    expect(card).toHaveTextContent("Yo");
    expect(card).toHaveTextContent("3 días en la etapa");
    expect(card).toHaveTextContent("Próxima acción: Llamar el viernes");
    const [cama] = within(column("Cotizado")).getAllByRole("listitem");
    expect(cama).toHaveTextContent("Cliente no visible"); // el contacto sin `contacts.view`
    expect(cama).toHaveTextContent("Otro miembro"); // sin `users.view` no se nombra
    expect(cama).toHaveTextContent("13 días en la etapa");
    expect(cama).toHaveTextContent("Sin próxima acción");
    expect(screen.getByText("3 oportunidades en la lista")).toBeInTheDocument();
    expect(screen.queryByText(/costo|cost/i)).toBeNull();
    expect(api).toHaveBeenCalledTimes(2); // pipelines y oportunidades; sin directorio
  });

  it("390 px: las columnas se desplazan en horizontal, con parada en cada una, y la región recibe el foco", async () => {
    mockApi({ [PIPELINES]: page([sales]), [LIST]: page([rocky]) });
    show();
    const board = await screen.findByRole("region", { name: "Tablero de oportunidades" });
    expect(board).toHaveClass("overflow-x-auto", "snap-x", "snap-mandatory");
    expect(board).toHaveAttribute("tabindex", "0");
    expect(column("Nuevo")).toHaveClass("shrink-0", "snap-start");
  });

  it("nombra al responsable con el directorio si la API deja leerlo, y lo ofrece en el filtro", async () => {
    mockApi({
      [PIPELINES]: page([sales]),
      [LIST]: page([luna]),
      [MEMBERS]: page([
        {
          ...{ id: "m2", status: "ACTIVE", joined_at: "2026-01-01T00:00:00Z", roles: [] },
          ...{ user: { id: "u2", email: "luis@acme.pe", first_name: "Luis", last_name: "Ramos" } },
          default_branch: null,
        },
      ]),
    });
    show(grants("opportunities.view", "users.view"));
    expect(await screen.findByText("Luis Ramos")).toBeVisible();
    const owner = screen.getByLabelText("Responsable");
    expect(
      within(owner)
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["Todos los responsables", "Yo", "Luis Ramos"]);
  });

  it("los filtros viajan a la API: pipeline, estado, responsable y búsqueda", async () => {
    const other = pipeline("p2", "Mayoristas", { stages: [stage("t1", "Contacto", 0)] });
    const api = mockApi({
      [PIPELINES]: page([other, sales]), // el predeterminado va primero aunque llegue después
      [LIST]: page([rocky]),
      "GET /api/v1/o/acme/opportunities/?pipeline_id=p1": page([rocky, luna]),
      "GET /api/v1/o/acme/opportunities/?pipeline_id=p1&status=OPEN&assigned_user_id=u1": page([
        rocky,
      ]),
      "GET /api/v1/o/acme/opportunities/?pipeline_id=p1&status=OPEN&assigned_user_id=u1&q=rocky":
        page([]),
      "GET /api/v1/o/acme/opportunities/?pipeline_id=p2&status=OPEN&assigned_user_id=u1&q=rocky":
        page([]),
    });
    show();
    await screen.findByRole("region", { name: "Tablero de oportunidades" });
    expect(screen.getByLabelText("Pipeline")).toHaveValue("p1");
    expect(screen.getByLabelText("Estado")).toHaveValue("OPEN");
    fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "" } });
    await waitFor(() => expect(column("Ganado")).toBeVisible()); // sin estado, también las ganadas
    fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "OPEN" } });
    fireEvent.change(screen.getByLabelText("Responsable"), { target: { value: "u1" } });
    await screen.findByRole("region", { name: "Tablero de oportunidades" });
    fireEvent.change(screen.getByLabelText("Buscar"), { target: { value: "rocky" } });
    expect(await screen.findByText("Ninguna oportunidad coincide con los filtros.")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Pipeline"), { target: { value: "p2" } });
    // Los filtros se conservan al cambiar de pipeline.
    const p2 =
      "/api/v1/o/acme/opportunities/?pipeline_id=p2&status=OPEN&assigned_user_id=u1&q=rocky";
    await waitFor(() => expect(api.mock.calls.map(([url]) => String(url))).toContain(p2));
  });

  it("la tabla enseña la misma lista, con la etapa en una columna, y el botón de vista lo dice", async () => {
    mockApi({ [PIPELINES]: page([sales]), [LIST]: page([rocky, luna]) });
    show();
    await screen.findByRole("region", { name: "Tablero de oportunidades" });
    const board = screen.getByRole("button", { name: "Tablero" });
    const table = screen.getByRole("button", { name: "Tabla" });
    expect(board).toHaveAttribute("aria-pressed", "true");
    expect(table).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(table);
    expect(table).toHaveAttribute("aria-pressed", "true");
    const grid = screen.getByRole("table");
    expect(
      within(grid)
        .getAllByRole("columnheader")
        .map((h) => h.textContent),
    ).toEqual([
      ...["Número", "Título", "Cliente", "Etapa", "Importe"],
      ...["Responsable", "Días en la etapa", "Próxima acción"],
    ]);
    const [, second] = within(grid).getAllByRole("row").slice(1);
    expect(
      within(second!)
        .getAllByRole("cell")
        .map((c) => c.textContent?.replace(/\s/g, " ")), // el importe separa «S/» con un espacio duro
    ).toEqual([
      ...["OPP-000002", "Cama Luna", "Cliente no visible", "Cotizado", "S/ 300.25"],
      ...["Otro miembro", "13", "Sin próxima acción"],
    ]);
    expect(screen.getByRole("region", { name: "Tabla de oportunidades" })).toHaveAttribute(
      "tabindex",
      "0",
    );
  });

  it("«Cargar más» sigue el cursor con los mismos filtros y avisa de que los totales son parciales", async () => {
    const api = mockApi({
      [PIPELINES]: page([sales]),
      [LIST]: page([rocky], "c-2"),
      [`${LIST}&cursor=c-2`]: page([luna]),
    });
    show();
    expect(await screen.findByText(/cuentan solo las cargadas/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("Cama Luna")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Cargar más" })).toBeNull();
    expect(api).toHaveBeenCalledTimes(3);
  });

  it("sin pipelines activos y sin oportunidades lo dice, sin tablero", async () => {
    mockApi({ [PIPELINES]: page([]) });
    show();
    expect(screen.getByText("Cargando oportunidades…")).toBeVisible();
    expect(await screen.findByText("Esta organización no tiene pipelines activos.")).toBeVisible();
    expect(screen.queryByLabelText("Pipeline")).toBeNull();
  });

  it("una lista vacía no es un error", async () => {
    mockApi({ [PIPELINES]: page([sales]), [LIST]: page([]) });
    show();
    expect(
      await screen.findByText("No hay oportunidades abiertas en este pipeline."),
    ).toBeVisible();
    expect(screen.queryByRole("region", { name: "Tablero de oportunidades" })).toBeNull();
  });

  it("un fallo se dice y se reintenta", async () => {
    let fail = true;
    mockApi({
      [PIPELINES]: page([sales]),
      [LIST]: () => (fail ? { status: 500, body: { code: "INTERNAL_ERROR" } } : page([rocky])),
    });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("Collar para Rocky")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("un 403 se explica y no se reintenta; la API decide quién ve las oportunidades", async () => {
    mockApi({ [PIPELINES]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver las oportunidades de esta organización.",
    );
    expect(screen.queryByRole("button", { name: "Reintentar" })).toBeNull();
  });
});
