import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Opportunity, Pipeline, SelfContext, Stage } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { useMoveOpportunity } from "./move-opportunity";
import { OpportunitiesBoard } from "./opportunities-board";

const PIPELINES = "GET /api/v1/o/acme/pipelines/?limit=200";
const MEMBERS = "GET /api/v1/o/acme/members/?limit=200";
const LIST = "GET /api/v1/o/acme/opportunities/?pipeline_id=p1&status=OPEN";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
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
  stage("s5", "Perdido", 4, { stage_type: "LOST" }),
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
  it("el título de cada tarjeta y de cada fila de la tabla abre el detalle de la oportunidad", async () => {
    mockApi({ [PIPELINES]: page([sales]), [LIST]: page([rocky]) });
    show();
    const card = await screen.findByRole("link", { name: "Collar para Rocky" });
    expect(card).toHaveAttribute("href", "/o/acme/oportunidades/1");
    fireEvent.click(screen.getByRole("button", { name: "Tabla" }));
    const row = within(await screen.findByRole("table")).getByRole("link", {
      name: "Collar para Rocky",
    });
    expect(row).toHaveAttribute("href", "/o/acme/oportunidades/1");
    // lo que escribió otra persona va aislado (`<bdi dir="auto">`), también en el título
    expect(row.querySelector("bdi")).toHaveAttribute("dir", "auto");
  });

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
    // 390 px: las columnas se desplazan en horizontal, con parada en cada una; la región, con foco.
    expect(board).toHaveClass("overflow-x-auto", "snap-x", "snap-mandatory");
    expect(board).toHaveAttribute("tabindex", "0");
    expect(column("Nuevo")).toHaveClass("shrink-0", "snap-start");
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

  it("la búsqueda manda como mucho 200 caracteres (el máximo de `q`) y nada con una sola letra", async () => {
    const api = mockApi({ [PIPELINES]: page([sales]), [LIST]: page([rocky]) });
    show();
    await screen.findByRole("region", { name: "Tablero de oportunidades" });
    fireEvent.change(screen.getByLabelText("Buscar"), { target: { value: "a" } });
    fireEvent.change(screen.getByLabelText("Buscar"), { target: { value: "ñ".repeat(250) } });
    await waitFor(() => expect(api).toHaveBeenCalledTimes(3)); // pipelines, lista y la búsqueda
    const last = new URL(String(api.mock.calls[2]![0]), "http://x");
    expect(last.searchParams.get("q")).toHaveLength(200);
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

  describe("mover a otra etapa (F5-05b)", () => {
    const MOVE = "POST /api/v1/o/acme/opportunities/1/move/";
    const MOVE2 = "POST /api/v1/o/acme/opportunities/2/move/";
    const OPEN_LIST = "/api/v1/o/acme/opportunities/?pipeline_id=p1&status=OPEN";
    const detail = { ...rocky, stage: luna.stage, version: 2, items: [], history: [] };
    const posts = (api: ReturnType<typeof mockApi>) =>
      api.mock.calls.filter(([, init]) => init?.method === "POST");
    const gate = () => {
      let open = () => {};
      return { open: () => open(), wait: new Promise<void>((resolve) => (open = resolve)) };
    };
    // Lo que "el servidor" tiene: la lista cambia si el test la cambia. La respuesta del POST
    // espera a `release`, así se ve la tarjeta ya movida sin respuesta.
    function setup(reply: { status: number; body?: unknown }, next: string | null = null) {
      const server = { rows: [rocky, luna, mimi] as unknown[] };
      const api = mockApi({
        [PIPELINES]: page([sales]),
        [LIST]: () => page(server.rows, next),
        [MOVE]: reply,
      });
      const send = api.getMockImplementation()!;
      const held = gate();
      api.mockImplementation(async (input, init) => {
        if (init?.method === "POST") await held.wait;
        return send(input, init);
      });
      document.cookie = "csrftoken=token-de-prueba";
      show();
      return { api, server, release: held.open };
    }
    const control = (title: string) => screen.getByRole("combobox", { name: new RegExp(title) });
    const go = (title: string, stage: string) => {
      fireEvent.change(control(title), { target: { value: stage } });
      fireEvent.click(screen.getByRole("button", { name: `Mover «${title}»` }));
    };
    const pick = async (stage: string) => {
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      go("Collar para Rocky", stage);
    };

    it("la tarjeta pasa a la columna nueva al instante, con sus totales; la respuesta trae la versión nueva y una segunda orden la usa", async () => {
      const { api, release } = setup({ status: 200, body: detail });
      await pick("s2");
      // Sin respuesta todavía: ya está en «Cotizado», y el control espera.
      expect(await within(column("Cotizado")).findByText("Collar para Rocky")).toBeVisible();
      expect(column("Nuevo")).toHaveTextContent("Sin oportunidades · S/ 0.00");
      expect(column("Cotizado")).toHaveTextContent("3 oportunidades · S/ 1,900.85");
      release();
      expect(await screen.findByText("«Collar para Rocky» pasó a «Cotizado».")).toBeVisible();
      expect(JSON.parse(String(posts(api)[0]![1]?.body))).toEqual({ stage_id: "s2", version: 1 });
      expect(control("Collar")).toHaveFocus(); // el foco sigue a la tarjeta
      expect(within(control("Collar")).queryByRole("option", { name: "Cotizado" })).toBeNull();
      // Liberada la orden anterior, otra sale, con la versión de la respuesta.
      go("Collar para Rocky", "s1");
      await waitFor(() => expect(posts(api)).toHaveLength(2));
      expect(JSON.parse(String(posts(api)[1]![1]?.body))).toEqual({ stage_id: "s1", version: 2 });
    });

    it.each([
      [409, "STALE_VERSION", "Otra persona cambió esta oportunidad antes que tú."],
      [409, "OPPORTUNITY_CLOSED", "Esta oportunidad ya está cerrada."],
      [409, "MISSING_FIELDS", "Esta oportunidad no tiene datos que la etapa exige."],
      [409, "PIPELINE_UNAVAILABLE", "El pipeline de esta oportunidad no está disponible."],
      [409, "CLOSING_STAGE", "Esta oportunidad no admite ese cambio ahora."],
      [400, "VALIDATION_ERROR", "Esa etapa ya no es válida para este pipeline."],
      [404, "NOT_FOUND", "Esta oportunidad ya no existe o salió de tu alcance."],
    ])(
      "%i %s: la tarjeta vuelve, lo dice y la lista se pide otra vez",
      async (status, code, said) => {
        const { api, release } = setup({ status, body: { code } });
        await pick("s2");
        expect(await within(column("Cotizado")).findByText("Collar para Rocky")).toBeVisible();
        release();
        expect(await screen.findByRole("alert")).toHaveTextContent(
          `${said} La tarjeta volvió a su etapa.`,
        );
        expect(within(column("Nuevo")).getByText("Collar para Rocky")).toBeVisible();
        expect(control("Collar")).toHaveFocus();
        await waitFor(() =>
          expect(api.mock.calls.filter(([url]) => String(url) === OPEN_LIST)).toHaveLength(2),
        );
      },
    );

    it("403: lo explica, la tarjeta vuelve, no se pide la lista, y una orden nueva borra el aviso", async () => {
      const { api, release } = setup({ status: 403, body: { code: "PERMISSION_DENIED" } });
      await pick("s2");
      release();
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "No tienes permiso para mover oportunidades.",
      );
      expect(within(column("Nuevo")).getByText("Collar para Rocky")).toBeVisible();
      expect(api).toHaveBeenCalledTimes(3); // pipelines, lista y el intento
      go("Collar para Rocky", "s2");
      expect(screen.queryByRole("alert")).toBeNull(); // el aviso anterior se va al empezar otra
      await screen.findByRole("alert");
    });

    it("si la tarjeta desaparece con la lista nueva, el foco va al aviso y no a la nada", async () => {
      const { server, release } = setup({ status: 404, body: { code: "NOT_FOUND" } });
      await pick("s2");
      server.rows = [luna, mimi]; // otra persona la borró
      release();
      const alert = await screen.findByRole("alert");
      await waitFor(() => expect(screen.queryByText("Collar para Rocky")).toBeNull());
      await waitFor(() => expect(alert).toHaveFocus());
    });

    it("«Mover» sin etapa elegida lo dice y no envía nada; sus botones se distinguen por la tarjeta", async () => {
      const { api } = setup({ status: 200, body: detail });
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      expect(screen.getAllByRole("button", { name: /^Mover «/ })).toHaveLength(3);
      const button = screen.getByRole("button", { name: "Mover «Collar para Rocky»" });
      fireEvent.click(button);
      expect(button).toHaveAttribute("aria-disabled", "true");
      expect(button).toHaveClass("aria-disabled:opacity-50");
      expect(screen.getByRole("alert")).toHaveTextContent("Elige primero la etapa");
      expect(control("Collar")).toHaveAccessibleDescription(/Elige primero la etapa/);
      expect(posts(api)).toHaveLength(0);
      // Etapas abiertas y activas menos la actual: ni «Ganado» (cerrada) ni «Antigua» (inactiva).
      expect(
        within(control("Collar"))
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["Mover a…", "Cotizado"]);
    });

    it("dos órdenes seguidas sobre la misma oportunidad envían una sola petición", async () => {
      const api = mockApi({ [MOVE]: { status: 200, body: detail } });
      document.cookie = "csrftoken=token-de-prueba";
      function Twice() {
        const mover = useMoveOpportunity("acme");
        const twice = () => [1, 2].forEach(() => void mover.move(rocky, sales.stages[1]!));
        return <button onClick={twice}>dos veces</button>;
      }
      renderApp(<Twice />);
      fireEvent.click(screen.getByRole("button", { name: "dos veces" }));
      await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    });

    it("«Cargar más» espera a que acabe la orden en curso", async () => {
      const { release } = setup({ status: 200, body: detail }, "c-2");
      await pick("s2");
      const more = screen.getByRole("button", { name: "Cargar más" });
      expect(more).toHaveAttribute("aria-disabled", "true");
      release();
      await screen.findByText("«Collar para Rocky» pasó a «Cotizado».");
      expect(more).toHaveAttribute("aria-disabled", "false");
    });

    it("una lista que llega tarde no deshace un movimiento en curso (se cancela la lectura)", async () => {
      let rows: unknown[] = [rocky, luna, mimi];
      const api = mockApi({
        [PIPELINES]: page([sales]),
        [LIST]: () => page(rows),
        [MOVE]: { status: 200, body: detail },
        [MOVE2]: { status: 409, body: { code: "STALE_VERSION" } },
      });
      const send = api.getMockImplementation()!;
      const late = gate();
      const slow = gate();
      let reads = 0;
      api.mockImplementation(async (input, init) => {
        if (String(input) === OPEN_LIST && ++reads === 2) {
          await late.wait; // la lectura que sigue al rechazo de A llega con la lista vieja
          return new Response(JSON.stringify({ results: [rocky, luna, mimi], next: null }));
        }
        if (String(input).includes("/1/move/")) await slow.wait;
        return send(input, init);
      });
      document.cookie = "csrftoken=token-de-prueba";
      show();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      go("Cama Luna", "s1"); // A: rechazada (409); la lista se pide otra vez
      await screen.findByRole("alert");
      await waitFor(() => expect(reads).toBe(2));
      go("Collar para Rocky", "s2"); // B: en curso
      await within(column("Cotizado")).findByText("Collar para Rocky");
      late.open();
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(within(column("Cotizado")).getByText("Collar para Rocky")).toBeVisible();
      rows = [{ ...detail, items: undefined }, luna, mimi];
      slow.open();
      await screen.findByText("«Collar para Rocky» pasó a «Cotizado».");
      await waitFor(() => expect(reads).toBe(3)); // al acabar la última orden, se relee
      expect(within(column("Cotizado")).getByText("Collar para Rocky")).toBeVisible();
    });
  });

  describe("crear una oportunidad (F5-05c)", () => {
    const CREATE = "POST /api/v1/o/acme/opportunities/";
    const OPEN_LIST = "/api/v1/o/acme/opportunities/?pipeline_id=p1&status=OPEN";
    const CONTACTS = "GET /api/v1/o/acme/contacts/?limit=8";
    const fresh = opportunity("9", {
      title: "Collar nuevo",
      amount: "99.50",
      next_action: "Llamar",
    });
    const created = { ...fresh, items: [], history: [] };
    const rosa = { id: "c1", display_name: "Rosa Díaz" };
    const posts = (api: ReturnType<typeof mockApi>) =>
      api.mock.calls.filter(([, init]) => init?.method === "POST");
    // `server.rows` es lo que "el servidor" lista; la respuesta de crear espera a `release`.
    // `gates`: la segunda lectura de la lista (la que sigue a crear) y mover la tarjeta 1 esperan.
    function setup(
      reply: { status: number; body?: unknown },
      codes = ["contacts.view"],
      gates: { refetch?: Promise<void>; move?: Promise<void> } = {},
    ) {
      const server = { rows: [rocky, luna, mimi] as unknown[], reads: 0 };
      const api = mockApi({
        "POST /api/v1/o/acme/opportunities/1/move/": {
          status: 200,
          body: { ...rocky, stage: luna.stage, version: 2, items: [], history: [] },
        },
        [PIPELINES]: page([sales]),
        [LIST]: () => page(server.rows),
        [CONTACTS]: page([rosa, { id: "c2", display_name: "Luis Paz" }]),
        [CREATE]: reply,
      });
      const send = api.getMockImplementation()!;
      let release = () => {};
      const held = new Promise<void>((resolve) => (release = resolve));
      api.mockImplementation(async (input, init) => {
        if (init?.method === "POST" && String(input).endsWith("/opportunities/")) await held;
        if (init?.method === "POST" && String(input).includes("/1/move/")) await gates.move;
        if (String(input) === OPEN_LIST && ++server.reads === 2) await gates.refetch;
        return send(input, init);
      });
      document.cookie = "csrftoken=token-de-prueba";
      show(grants("opportunities.view", ...codes));
      return { api, server, release };
    }
    const open = async () => {
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.click(screen.getByRole("button", { name: "Nueva oportunidad" }));
      return screen.getByRole("form", { name: "Nueva oportunidad" });
    };
    const fill = (label: string, value: string) =>
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    const submit = () => fireEvent.click(screen.getByRole("button", { name: "Crear oportunidad" }));
    async function complete() {
      await open();
      fill("Título", "Collar nuevo");
      fill("Importe estimado (opcional)", "99.5");
      fill("Próxima acción (opcional)", "Llamar");
      fireEvent.click(await screen.findByRole("radio", { name: "Rosa Díaz" }));
      submit();
    }

    it("crea en el pipeline elegido, la tarjeta nueva aparece en la primera etapa con los totales y recibe el foco", async () => {
      const { api, server, release } = setup({ status: 201, body: created });
      await complete();
      expect(screen.getByRole("button", { name: "Creando…" })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
      server.rows = [rocky, luna, mimi, fresh];
      release();
      expect(await screen.findByText("«Collar nuevo» creada en «Nuevo».")).toBeVisible();
      expect(JSON.parse(String(posts(api)[0]![1]?.body))).toEqual({
        ...{ contact_id: "c1", title: "Collar nuevo", pipeline_id: "p1" },
        ...{ amount: "99.5", next_action: "Llamar" },
      });
      expect(screen.queryByRole("form", { name: "Nueva oportunidad" })).toBeNull();
      expect(column("Nuevo")).toHaveTextContent("2 oportunidades · S/ 1,600.00");
      await waitFor(() => expect(document.getElementById("opportunity-9")).toHaveFocus());
    });

    it("si un filtro o una página sin cargar la oculta, lo dice y el foco vuelve al botón", async () => {
      const { release } = setup({ status: 201, body: created }); // la lista no la trae
      await complete();
      release();
      expect(
        await screen.findByText(/pero los filtros actuales o las páginas sin cargar/),
      ).toBeVisible();
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Nueva oportunidad" })).toHaveFocus(),
      );
    });

    it.each([
      [
        { status: 400, body: { code: "VALIDATION_ERROR", fields: { title: ["x"] } } },
        "El título no es válido",
      ],
      [
        { status: 400, body: { code: "VALIDATION_ERROR", fields: { amount: ["x"] } } },
        "El importe no es válido",
      ],
      [
        { status: 400, body: { code: "VALIDATION_ERROR", fields: { next_action: ["x"] } } },
        "La próxima acción no es válida",
      ],
      [
        { status: 400, body: { code: "VALIDATION_ERROR", fields: { contact_id: ["x"] } } },
        "Ese cliente no existe o no está a tu alcance",
      ],
      [
        { status: 400, body: { code: "VALIDATION_ERROR", fields: { pipeline_id: ["x"] } } },
        "Este pipeline no está disponible o no tiene una etapa abierta",
      ],
      [
        { status: 409, body: { code: "PIPELINE_UNAVAILABLE" } },
        "No se pudo crear: el pipeline no tiene una primera etapa",
      ],
      [
        { status: 403, body: { code: "PERMISSION_DENIED" } },
        "No tienes permiso para crear oportunidades.",
      ],
      [{ status: 404, body: { code: "NOT_FOUND" } }, "No encontramos lo que buscas."],
      [{ status: 500, body: { code: "INTERNAL_ERROR" } }, "Algo salió mal de nuestro lado"],
    ])(
      "rechazo %#: lo dice junto a su campo o en el formulario y conserva lo escrito",
      async (reply, said) => {
        const { release } = setup(reply);
        await complete();
        release();
        await waitFor(() => expect(screen.getByRole("form")).toHaveTextContent(said));
        expect(screen.getByLabelText("Título")).toHaveValue("Collar nuevo"); // lo escrito se conserva
        // Un cliente que la API rechaza deja de figurar como elegido.
        expect((screen.getByRole("radio", { name: "Rosa Díaz" }) as HTMLInputElement).checked).toBe(
          !said.startsWith("Ese cliente"),
        );
        expect(screen.getByRole("button", { name: "Crear oportunidad" })).toBeVisible(); // se puede reintentar
      },
    );

    it("sin título, con un importe inválido o sin cliente no envía nada", async () => {
      const { api } = setup({ status: 201, body: created });
      await open();
      submit();
      expect(screen.getByText("Escribe el título de la oportunidad.")).toBeVisible();
      fill("Título", "Collar nuevo");
      fill("Importe estimado (opcional)", "1,5");
      submit();
      expect(screen.getByText(/El importe no es válido/)).toBeVisible();
      fill("Importe estimado (opcional)", "0");
      submit();
      expect(screen.getByText("Elige un cliente.")).toBeVisible();
      expect(posts(api)).toHaveLength(0);
    });

    it("sin `contacts.view` explica que no se puede elegir un cliente y no ofrece la búsqueda", async () => {
      const { api } = setup({ status: 201, body: created }, []);
      await open();
      expect(screen.getByText(/necesitas poder ver los clientes/)).toBeVisible();
      expect(screen.queryByLabelText("Buscar cliente")).toBeNull();
      fill("Título", "Collar nuevo");
      submit();
      expect(screen.getByText("Elige un cliente.")).toBeVisible();
      expect(api.mock.calls.some(([url]) => String(url).includes("/contacts/"))).toBe(false);
    });

    it("la búsqueda de cliente usa la de Clientes y Enter en ella no envía el formulario", async () => {
      const { api } = setup({ status: 201, body: created });
      await open();
      const search = await screen.findByLabelText("Buscar cliente");
      expect(fireEvent.keyDown(search, { key: "Enter" })).toBe(false); // se previene el envío
      fireEvent.change(search, { target: { value: "rosa" } });
      await waitFor(() =>
        expect(api.mock.calls.map(([url]) => String(url))).toContain(
          "/api/v1/o/acme/contacts/?limit=8&q=rosa",
        ),
      );
      expect(posts(api)).toHaveLength(0);
    });

    it("dos envíos seguidos son una sola petición", async () => {
      const { api, release } = setup({ status: 201, body: created });
      await complete();
      const form = screen.getByRole("form", { name: "Nueva oportunidad" });
      fireEvent.submit(form);
      fireEvent.submit(form);
      release();
      await screen.findByText(/creada en|se creó en/);
      expect(posts(api)).toHaveLength(1);
    });

    it("Escape cierra sin enviar y devuelve el foco; Enter en el título envía", async () => {
      const { api, release } = setup({ status: 201, body: created });
      const form = await open();
      fireEvent.keyDown(screen.getByLabelText("Título"), { key: "Escape" });
      expect(screen.queryByRole("form", { name: "Nueva oportunidad" })).toBeNull();
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Nueva oportunidad" })).toHaveFocus(),
      );
      expect(posts(api)).toHaveLength(0);
      expect(form).not.toBeInTheDocument();
      await complete();
      release();
      await screen.findByText(/creada en|se creó en/);
      expect(posts(api)).toHaveLength(1);
    });

    const gate = () => {
      let open = () => {};
      return { open: () => open(), wait: new Promise<void>((resolve) => (open = resolve)) };
    };

    it("solo manda lo escrito: sin importe ni próxima acción no viajan", async () => {
      const { api, release } = setup({ status: 201, body: created });
      await open();
      fill("Título", "Collar nuevo");
      fireEvent.click(await screen.findByRole("radio", { name: "Luis Paz" }));
      submit();
      release();
      await screen.findByText(/creada en|se creó en/);
      expect(JSON.parse(String(posts(api)[0]![1]?.body))).toEqual({
        ...{ contact_id: "c2", title: "Collar nuevo", pipeline_id: "p1" },
      });
    });

    it("no le quita el foco a quien se fue a otra parte mientras se relee la lista", async () => {
      const refetch = gate();
      const { server, release } = setup({ status: 201, body: created }, ["contacts.view"], {
        refetch: refetch.wait,
      });
      await complete();
      release();
      await waitFor(() => expect(server.reads).toBe(2)); // la lectura posterior a crear espera
      server.rows = [rocky, luna, mimi, fresh];
      screen.getByLabelText("Buscar").focus(); // el filtro del tablero
      refetch.open();
      await screen.findByText("«Collar nuevo» creada en «Nuevo».");
      await new Promise((resolve) => setTimeout(resolve, 20));
      await waitFor(() => expect(screen.getByLabelText("Buscar")).toHaveFocus());
    });

    it("espera a que acabe un movimiento en curso antes de releer la lista", async () => {
      const move = gate();
      const { server, release } = setup({ status: 201, body: created }, ["contacts.view"], {
        move: move.wait,
      });
      await complete();
      fireEvent.change(screen.getByRole("combobox", { name: /Mover «Collar para Rocky»/ }), {
        target: { value: "s2" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Mover «Collar para Rocky»" }));
      release();
      await new Promise((resolve) => setTimeout(resolve, 120));
      expect(server.reads).toBe(1); // sin lectura nueva mientras el movimiento no acaba
      server.rows = [rocky, luna, mimi, fresh];
      move.open();
      expect(await screen.findByText("«Collar nuevo» creada en «Nuevo».")).toBeVisible();
    });

    it("un movimiento que empieza durante la lectura la cancela: se espera y se relee, no dice «oculta»", async () => {
      const refetch = gate();
      const move = gate();
      const { server, release } = setup({ status: 201, body: created }, ["contacts.view"], {
        refetch: refetch.wait,
        move: move.wait,
      });
      await complete();
      release();
      await waitFor(() => expect(server.reads).toBe(2));
      server.rows = [rocky, luna, mimi, fresh];
      fireEvent.change(screen.getByRole("combobox", { name: /Mover «Collar para Rocky»/ }), {
        target: { value: "s2" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Mover «Collar para Rocky»" }));
      refetch.open(); // la lectura cancelada no vale
      await new Promise((resolve) => setTimeout(resolve, 120));
      expect(screen.queryByText(/pero los filtros/)).toBeNull();
      move.open();
      expect(await screen.findByText("«Collar nuevo» creada en «Nuevo».")).toBeVisible();
    });

    it("si no se puede releer la lista, lo dice sin culpar a los filtros", async () => {
      const { api, release } = setup({ status: 201, body: created });
      const send = api.getMockImplementation()!;
      let reads = 0;
      api.mockImplementation(async (input, init) =>
        String(input) === OPEN_LIST && ++reads >= 2
          ? new Response(JSON.stringify({ code: "INTERNAL_ERROR" }), { status: 500 })
          : send(input, init),
      );
      await complete();
      release();
      expect(await screen.findByText(/no pudimos actualizar la lista/)).toBeVisible();
      expect(screen.queryByText(/pero los filtros/)).toBeNull();
    });

    it("sin una etapa abierta y activa el botón lo explica y no abre el formulario", async () => {
      const closed = pipeline("p3", "Cerrado", {
        ...{ is_default: true, stages: [stage("w", "Ganado", 0, { stage_type: "WON" })] },
      });
      mockApi({
        [PIPELINES]: page([closed]),
        "GET /api/v1/o/acme/opportunities/?pipeline_id=p3&status=OPEN": page([]),
      });
      show();
      const button = await screen.findByRole("button", { name: "Nueva oportunidad" });
      fireEvent.click(button);
      expect(button).toHaveAttribute("aria-disabled", "true");
      expect(button).toHaveAccessibleDescription(/no tiene una etapa abierta y activa/);
      expect(screen.queryByRole("form", { name: "Nueva oportunidad" })).toBeNull();
    });

    it("Escape en la búsqueda de cliente o durante una composición no cierra el formulario", async () => {
      setup({ status: 201, body: created });
      await open();
      fill("Título", "Collar nuevo");
      fireEvent.keyDown(await screen.findByLabelText("Buscar cliente"), { key: "Escape" });
      fireEvent.keyDown(screen.getByLabelText("Título"), { key: "Escape", isComposing: true });
      expect(screen.getByLabelText("Título")).toHaveValue("Collar nuevo");
    });

    it("sin cliente, el error se asocia a los clientes y el foco va a la búsqueda", async () => {
      setup({ status: 201, body: created });
      await open();
      fill("Título", "Collar nuevo");
      submit();
      const search = await screen.findByLabelText("Buscar cliente");
      await waitFor(() => expect(search).toHaveFocus());
      expect(screen.getByRole("group", { name: "Cliente" })).toHaveAccessibleDescription(
        "Elige un cliente.",
      );
      fireEvent.click(await screen.findByRole("radio", { name: "Rosa Díaz" }));
      expect(screen.getByText("Cliente elegido: Rosa Díaz")).toBeVisible();
    });

    it("al reabrir tras crear o cancelar, el formulario empieza limpio; Enter mantenido no repite", async () => {
      const { release } = setup({ status: 201, body: created });
      await complete();
      release();
      await screen.findByText(/creada en|se creó en/);
      fireEvent.click(screen.getByRole("button", { name: "Nueva oportunidad" }));
      expect(screen.getByLabelText("Título")).toHaveValue("");
      expect(screen.queryByRole("radio", { checked: true })).toBeNull();
      expect(screen.queryByText("Elige un cliente.")).toBeNull();
      expect(screen.queryByText(/creada en/)).toBeNull(); // el aviso anterior se va
      fill("Título", "Otro");
      fireEvent.click(await screen.findByRole("radio", { name: "Rosa Díaz" }));
      expect(
        fireEvent.keyDown(screen.getByLabelText("Título"), { key: "Enter", repeat: true }),
      ).toBe(false);
      fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
      fireEvent.click(screen.getByRole("button", { name: "Nueva oportunidad" }));
      expect(screen.getByLabelText("Título")).toHaveValue("");
      expect(screen.queryByRole("radio", { checked: true })).toBeNull();
    });
  });

  describe("ganar, perder y reabrir (F5-05d)", () => {
    const REASONS = "GET /api/v1/o/acme/lost-reasons/?limit=200";
    const reason = (id: string, name: string, requires_note = false, is_active = true) => ({
      ...{ id, name, code: id.toUpperCase(), sort_order: 0, is_active, requires_note, version: 1 },
    });
    const reasons = [
      reason("r1", "Precio"),
      reason("r2", "Otro", true),
      reason("r3", "Vieja", false, false),
    ];
    const closedStage = { id: "s4", name: "Ganado", type: "WON" } as const;
    const won = { ...rocky, status: "WON", stage: closedStage, amount: "1800.00", version: 2 };
    const detail = { ...won, items: [], history: [] };
    const lostCard = opportunity("5", {
      ...{
        title: "Cama perdida",
        status: "LOST",
        stage: { id: "s5", name: "Perdido", type: "LOST" },
      },
      lost_reason: { id: "r1", name: "Precio" },
    });
    const wonCard = opportunity("4", { title: "Cama ganada", status: "WON", stage: closedStage });
    const WIN = "POST /api/v1/o/acme/opportunities/1/win/";
    const LOSE = "POST /api/v1/o/acme/opportunities/1/lose/";
    const posts = (api: ReturnType<typeof mockApi>) =>
      api.mock.calls.filter(([, init]) => init?.method === "POST");
    const body = (api: ReturnType<typeof mockApi>) => JSON.parse(String(posts(api)[0]![1]?.body));
    const gate = () => {
      let open = () => {};
      return { open: () => open(), wait: new Promise<void>((resolve) => (open = resolve)) };
    };
    // `hold(url)`: las órdenes POST a esa dirección esperan; `rows`: lo que "el servidor" lista.
    function setup(
      routes: Parameters<typeof mockApi>[0] = {},
      hold: (url: string) => Promise<void> | undefined = () => undefined,
      codes = ["opportunities.view"],
    ) {
      const server = { rows: [rocky, luna, mimi] as unknown[] };
      const api = mockApi({
        [PIPELINES]: page([sales]),
        [LIST]: () => page(server.rows),
        "GET /api/v1/o/acme/opportunities/?pipeline_id=p1": page([wonCard, lostCard, rocky]),
        [REASONS]: page(reasons),
        ...routes,
      });
      const send = api.getMockImplementation()!;
      api.mockImplementation(async (input, init) => {
        if (init?.method === "POST") await hold(String(input));
        return send(input, init);
      });
      document.cookie = "csrftoken=token-de-prueba";
      show(grants(...codes));
      return { api, server };
    }
    const ready = () => screen.findByRole("region", { name: "Tablero de oportunidades" });
    const open = async (button: string, title = "Collar para Rocky") => {
      await ready();
      fireEvent.click(screen.getByRole("button", { name: `${button} «${title}»` }));
    };
    const submit = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
    const setAmount = (value: string) =>
      fireEvent.change(screen.getByLabelText("Importe final"), { target: { value } });

    it("ganar pide el importe final (con el actual) y la tarjeta sale de las abiertas, con totales y aviso", async () => {
      const { api, server } = setup({ [WIN]: { status: 200, body: detail } });
      await open("Ganar");
      expect(screen.getByLabelText("Importe final")).toHaveValue("1500.50");
      setAmount("1800");
      server.rows = [luna, mimi]; // el servidor ya no la lista entre las abiertas
      submit("Ganar oportunidad");
      expect(await screen.findByText("«Collar para Rocky» ganada por S/ 1,800.00.")).toBeVisible();
      expect(body(api)).toEqual({ amount: "1800", version: 1 });
      await waitFor(() => expect(screen.queryByText("Collar para Rocky")).toBeNull());
      expect(column("Nuevo")).toHaveTextContent("Sin oportunidades · S/ 0.00");
      // La lista se relee (ya no cuenta la ganada) y el foco no se pierde: va al aviso del tablero.
      expect(await screen.findByText("2 oportunidades en la lista")).toBeInTheDocument();
      await waitFor(() => expect(document.activeElement).toHaveAttribute("role", "status"));
      expect(document.activeElement).toHaveTextContent("ganada por");
    });

    it.each([["0"], ["-5"], ["1,5"], ["1.234"], [".5"], ["1234567890123"], [" "]])(
      "ganar con el importe «%s» no envía nada",
      async (value) => {
        const { api } = setup({ [WIN]: { status: 200, body: detail } });
        await open("Ganar");
        setAmount(value);
        submit("Ganar oportunidad");
        expect(screen.getByRole("form", { name: "Ganar la oportunidad" })).toHaveTextContent(
          /Escribe el importe final|El importe no es válido/,
        );
        expect(posts(api)).toHaveLength(0);
      },
    );

    it("perder: el motivo es obligatorio, las inactivas no se ofrecen y la nota se omite si está vacía", async () => {
      const { api } = setup({ [LOSE]: { status: 200, body: { ...detail, status: "LOST" } } });
      await ready();
      expect(api.mock.calls.some(([url]) => String(url).includes("lost-reasons"))).toBe(false); // aún no
      fireEvent.click(screen.getByRole("button", { name: "Perder «Collar para Rocky»" }));
      expect(await screen.findByRole("radio", { name: "Precio" })).toBeVisible();
      expect(screen.queryByRole("radio", { name: /Vieja/ })).toBeNull();
      submit("Perder oportunidad");
      expect(screen.getByText("Elige un motivo.")).toBeVisible();
      expect(
        screen.getByRole("group", { name: "Motivo de la pérdida" }),
      ).toHaveAccessibleDescription("Elige un motivo.");
      expect(posts(api)).toHaveLength(0);
      fireEvent.click(screen.getByRole("radio", { name: "Precio" }));
      submit("Perder oportunidad");
      expect(await screen.findByText("«Collar para Rocky» perdida: Precio.")).toBeVisible();
      expect(body(api)).toEqual({ lost_reason_id: "r1", version: 1 });
    });

    it("un motivo que exige nota no deja perder sin ella (ni con espacios) y con ella la envía", async () => {
      const { api } = setup({ [LOSE]: { status: 200, body: { ...detail, status: "LOST" } } });
      await open("Perder");
      fireEvent.click(await screen.findByRole("radio", { name: /Otro/ }));
      for (const note of ["", "   "]) {
        fireEvent.change(screen.getByLabelText("Nota"), { target: { value: note } });
        submit("Perder oportunidad");
        expect(screen.getByText("Este motivo exige una nota: escríbela.")).toBeVisible();
      }
      expect(posts(api)).toHaveLength(0);
      expect(screen.getByLabelText("Nota")).toHaveAttribute("maxLength", "1000");
      fireEvent.change(screen.getByLabelText("Nota"), {
        target: { value: " Compró en otra tienda " },
      });
      submit("Perder oportunidad");
      await screen.findByText(/perdida: Otro/);
      expect(body(api)).toEqual({
        lost_reason_id: "r2",
        note: "Compró en otra tienda",
        version: 1,
      });
    });

    it.each([
      [
        { status: 403, body: { code: "PERMISSION_DENIED" } },
        /No tienes permiso para ver los motivos/,
      ],
      [
        { status: 200, body: { results: [reason("r3", "Vieja", false, false)], next: null } },
        /No hay motivos de pérdida activos/,
      ],
    ])(
      "una lista de motivos negada o sin activos se explica sin bloquear nada más (%#)",
      async (reply, said) => {
        setup({ [REASONS]: reply });
        await open("Perder");
        expect(await screen.findByText(said)).toBeVisible();
        expect(screen.getByRole("button", { name: "Cancelar" })).toBeVisible();
      },
    );

    it.each([
      [WIN, 409, "STALE_VERSION", "Otra persona cambió esta oportunidad antes que tú."],
      [WIN, 409, "OPPORTUNITY_CLOSED", "Esta oportunidad ya está cerrada."],
      [WIN, 409, "MISSING_FIELDS", "Esta oportunidad no tiene datos que la etapa exige."],
      [WIN, 409, "PIPELINE_UNAVAILABLE", "El pipeline de esta oportunidad no está disponible."],
      [WIN, 409, "OTHER", "Esta oportunidad no admite ese cambio ahora."],
      [WIN, 404, "NOT_FOUND", "Esta oportunidad ya no existe o salió de tu alcance."],
      [LOSE, 409, "STALE_VERSION", "Otra persona cambió esta oportunidad antes que tú."],
    ])(
      "%s %i %s: lo dice el tablero, se cierra el formulario y la lista se pide otra vez",
      async (route, status, code, said) => {
        const { api } = setup({ [route]: { status, body: { code } } });
        await open(route === WIN ? "Ganar" : "Perder");
        if (route === LOSE) fireEvent.click(await screen.findByRole("radio", { name: "Precio" }));
        submit(route === WIN ? "Ganar oportunidad" : "Perder oportunidad");
        expect(await screen.findByRole("alert")).toHaveTextContent(said);
        await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
        await waitFor(() =>
          expect(
            api.mock.calls.filter(
              ([url]) => String(url) === "/api/v1/o/acme/opportunities/?pipeline_id=p1&status=OPEN",
            ),
          ).toHaveLength(2),
        );
      },
    );

    const bad = (field: string) => ({
      ...{ status: 400, body: { code: "VALIDATION_ERROR", fields: { [field]: ["x"] } } },
    });
    const denied = { status: 403, body: { code: "PERMISSION_DENIED" } };
    const noPermission = "No tienes permiso para esta acción.";
    it.each([
      [WIN, bad("amount"), "El importe no es válido"],
      [LOSE, bad("note"), "La nota no es válida"],
      [LOSE, bad("lost_reason_id"), "Ese motivo ya no está disponible"],
      [WIN, denied, noPermission],
      [LOSE, denied, noPermission],
      [WIN, { status: 500, body: { code: "INTERNAL_ERROR" } }, "Algo salió mal de nuestro lado"],
    ])("%s rechazo %#: el formulario sigue abierto con lo escrito", async (route, reply, said) => {
      const { api } = setup({ [route]: reply });
      await open(route === WIN ? "Ganar" : "Perder");
      if (route === WIN) setAmount("1800");
      else {
        fireEvent.click(await screen.findByRole("radio", { name: "Precio" }));
        fireEvent.change(screen.getByLabelText("Nota (opcional)"), { target: { value: "algo" } });
      }
      submit(route === WIN ? "Ganar oportunidad" : "Perder oportunidad");
      await waitFor(() => expect(screen.getByRole("form")).toHaveTextContent(said));
      expect(screen.getByRole("form")).toBeVisible();
      if (route === WIN) expect(screen.getByLabelText("Importe final")).toHaveValue("1800");
      else expect(screen.getByLabelText(/^Nota/)).toHaveValue("algo");
      expect(posts(api)).toHaveLength(1);
    });

    it("dos envíos seguidos son una sola petición, y Escape no cierra mientras se envía", async () => {
      const held = gate();
      const { api } = setup({ [WIN]: { status: 200, body: detail } }, (url) =>
        url.includes("/win/") ? held.wait : undefined,
      );
      await open("Ganar");
      const form = screen.getByRole("form", { name: "Ganar la oportunidad" });
      fireEvent.submit(form);
      fireEvent.submit(form);
      await screen.findByRole("button", { name: "Enviando…" });
      fireEvent.keyDown(screen.getByLabelText("Importe final"), { key: "Escape" });
      expect(screen.getByRole("form", { name: "Ganar la oportunidad" })).toBeVisible();
      held.open();
      await screen.findByText(/ganada por/);
      expect(posts(api)).toHaveLength(1);
    });

    it("Escape cierra sin enviar y devuelve el foco al botón; no durante una composición", async () => {
      const { api } = setup({ [WIN]: { status: 200, body: detail } });
      await open("Ganar");
      fireEvent.keyDown(screen.getByLabelText("Importe final"), {
        key: "Escape",
        isComposing: true,
      });
      expect(screen.getByRole("form", { name: "Ganar la oportunidad" })).toBeVisible();
      fireEvent.keyDown(screen.getByLabelText("Importe final"), { key: "Escape" });
      expect(screen.queryByRole("form")).toBeNull();
      expect(screen.getByRole("button", { name: "Ganar «Collar para Rocky»" })).toHaveFocus();
      expect(posts(api)).toHaveLength(0);
    });

    it("con un movimiento de la misma tarjeta en curso, ganar y perder esperan", async () => {
      const held = gate();
      const { api } = setup({ [WIN]: { status: 200, body: detail } }, (url) =>
        url.includes("/1/move/") ? held.wait : undefined,
      );
      await ready();
      fireEvent.change(screen.getByRole("combobox", { name: /Mover «Collar para Rocky»/ }), {
        target: { value: "s2" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Mover «Collar para Rocky»" }));
      const button = await screen.findByRole("button", { name: "Ganar «Collar para Rocky»" });
      fireEvent.click(button);
      expect(button).toHaveAttribute("aria-disabled", "true");
      expect(screen.queryByRole("form")).toBeNull();
      held.open();
      expect(posts(api).filter(([url]) => String(url).includes("/win/"))).toHaveLength(0);
    });

    it("las ganadas y perdidas dicen su estado, no se mueven ni se ganan, y se reabren en una etapa abierta", async () => {
      const opened = { ...wonCard, status: "OPEN", stage: luna.stage, version: 2 };
      let closed: unknown[] = [wonCard]; // lo que el servidor lista como ganadas
      const { api } = setup({
        "POST /api/v1/o/acme/opportunities/4/reopen/": {
          status: 200,
          body: { ...opened, items: [], history: [] },
        },
        "GET /api/v1/o/acme/opportunities/?pipeline_id=p1&status=WON": () => page(closed),
      });
      await ready();
      fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "" } });
      expect(await screen.findByText("Cama ganada")).toBeVisible();
      expect(screen.getByText("Ganada")).toBeVisible();
      expect(screen.getByText("Perdida: Precio")).toBeVisible();
      expect(screen.queryByRole("button", { name: /^Ganar «Cama/ })).toBeNull();
      expect(screen.queryByRole("combobox", { name: /^Mover «Cama/ })).toBeNull();
      // Bajo «Ganadas»: al reabrirla sale de la lista (se relee), lo dice y el foco no se pierde.
      fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "WON" } });
      const select = await screen.findByRole("combobox", {
        name: "Reabrir «Cama ganada» en otra etapa",
      });
      expect(
        within(select)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["Reabrir en…", "Nuevo", "Cotizado"]);
      closed = [];
      fireEvent.change(select, { target: { value: "s2" } });
      fireEvent.click(screen.getByRole("button", { name: "Reabrir «Cama ganada»" }));
      expect(
        await screen.findByText("Todavía no hay oportunidades ganadas en este pipeline."),
      ).toBeVisible();
      expect(body(api)).toEqual({ stage_id: "s2", version: 1 });
      await waitFor(() => expect(document.activeElement).toHaveAttribute("role", "status"));
      expect(document.activeElement).toHaveTextContent("«Cama ganada» se reabrió en «Cotizado».");
    });

    it("los motivos de pérdida se ofrecen en el orden de `sort_order`", async () => {
      const second = { ...reason("r4", "Segundo"), sort_order: 2 };
      const first = { ...reason("r5", "Primero"), sort_order: 1 };
      setup({ [REASONS]: { status: 200, body: { results: [second, first], next: null } } });
      await open("Perder");
      await screen.findByRole("radio", { name: "Primero" });
      expect(screen.getAllByRole("radio").map((r) => r.closest("label")?.textContent)).toEqual([
        "Primero",
        "Segundo",
      ]);
    });
  });

  describe("arrastrar a otra etapa (F5-05e)", () => {
    const MOVE = "POST /api/v1/o/acme/opportunities/1/move/";
    const OPEN_LIST = "GET /api/v1/o/acme/opportunities/?pipeline_id=p1&status=OPEN";
    const ALL_LIST = "GET /api/v1/o/acme/opportunities/?pipeline_id=p1";
    const detail = { ...rocky, stage: luna.stage, version: 2, items: [], history: [] };
    const won = opportunity("4", {
      ...{ title: "Cama ganada", status: "WON", stage: { id: "s4", name: "Ganado", type: "WON" } },
    });
    const old = opportunity("6", {
      ...{ title: "Cama vieja", stage: { id: "s3", name: "Antigua", type: "OPEN" } },
    });
    const posts = (api: ReturnType<typeof mockApi>) =>
      api.mock.calls.filter(([, init]) => init?.method === "POST");
    function setup(reply = { status: 200, body: detail } as { status: number; body?: unknown }) {
      const api = mockApi({
        [PIPELINES]: page([sales]),
        [OPEN_LIST]: page([rocky, luna, mimi]),
        [ALL_LIST]: page([rocky, luna, won, old]),
        [MOVE]: reply,
      });
      document.cookie = "csrftoken=token-de-prueba";
      show();
      return api;
    }
    const handle = (title = "Collar para Rocky") =>
      screen.getByRole("button", { name: `Arrastrar «${title}» a otra etapa` });
    // jsdom no calcula posiciones: lo que hay bajo el puntero lo dice cada prueba.
    const under = (stage: string | null) => {
      const target = stage
        ? within(column(stage)).getByRole("heading", { name: stage })
        : document.body;
      document.elementFromPoint = () => target;
    };
    const grab = async () => {
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.pointerDown(handle(), {
        button: 0,
        isPrimary: true,
        pointerId: 1,
        clientX: 10,
        clientY: 10,
      });
    };
    const move = (x = 80, y = 80) =>
      fireEvent.pointerMove(handle(), { pointerId: 1, clientX: x, clientY: y });
    const said = (text: string) => screen.findByText(text);

    it("arrastrar con el puntero: la columna bajo él se resalta y al soltar se envía el cambio", async () => {
      const api = setup();
      await grab();
      under("Cotizado");
      move();
      expect(await said("Etapa «Cotizado»: suéltala aquí.")).toBeInTheDocument();
      expect(column("Cotizado")).toHaveClass("bg-accent/30", "border-solid");
      expect(column("Nuevo")).not.toHaveClass("border-dashed"); // la actual no es destino
      expect(handle()).toHaveAttribute("aria-pressed", "true");
      fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 80, clientY: 80 });
      expect(await said("Moviendo «Collar para Rocky» a «Cotizado»…")).toBeInTheDocument();
      expect(await screen.findByText("«Collar para Rocky» pasó a «Cotizado».")).toBeVisible();
      expect(JSON.parse(String(posts(api)[0]![1]?.body))).toEqual({ stage_id: "s2", version: 1 });
      expect(within(column("Cotizado")).getByText("Collar para Rocky")).toBeVisible();
    });

    it("soltar fuera de una columna, en la misma o en una cerrada no envía nada; Escape cancela", async () => {
      const api = setup();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "" } });
      await screen.findByText("Cama ganada");
      for (const place of [null, "Nuevo", "Ganado"]) {
        fireEvent.pointerDown(handle(), {
          button: 0,
          isPrimary: true,
          pointerId: 1,
          clientX: 10,
          clientY: 10,
        });
        under(place);
        move();
        fireEvent.pointerUp(handle(), { pointerId: 1 });
        expect(
          await said("Movimiento cancelado: «Collar para Rocky» sigue en «Nuevo»."),
        ).toBeInTheDocument();
      }
      fireEvent.pointerDown(handle(), {
        button: 0,
        isPrimary: true,
        pointerId: 1,
        clientX: 10,
        clientY: 10,
      });
      under("Cotizado");
      move();
      fireEvent.keyDown(window, { key: "Escape" });
      fireEvent.pointerUp(handle(), { pointerId: 1 });
      expect(handle()).toHaveAttribute("aria-pressed", "false");
      await new Promise((resolve) => setTimeout(resolve, 30));
      expect(posts(api)).toHaveLength(0);
    });

    it("un clic en el asa sin moverlo no es un arrastre", async () => {
      const api = setup();
      await grab();
      fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 10, clientY: 10 });
      expect(screen.queryByText(/cancelado|tomada/)).toBeNull();
      expect(posts(api)).toHaveLength(0);
    });

    it("con el teclado: tomar, elegir etapa, soltar y cancelar, con sus anuncios", async () => {
      const api = setup();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.keyDown(handle(), { key: " " });
      expect(
        await said(
          "«Collar para Rocky» tomada, en «Nuevo». Flecha izquierda o derecha para elegir la etapa, Espacio o Intro para soltar, Escape para cancelar.",
        ),
      ).toBeInTheDocument();
      expect(handle()).toHaveAttribute("aria-pressed", "true");
      fireEvent.keyDown(handle(), { key: "ArrowLeft" });
      expect(await said("No hay más etapas en esa dirección.")).toBeInTheDocument();
      fireEvent.keyDown(handle(), { key: "ArrowRight" });
      expect(await said("Etapa «Cotizado»: suéltala aquí.")).toBeInTheDocument();
      expect(column("Cotizado")).toHaveClass("bg-accent/30");
      fireEvent.keyDown(handle(), { key: "Enter" });
      expect(await said("Moviendo «Collar para Rocky» a «Cotizado»…")).toBeInTheDocument();
      await screen.findByText("«Collar para Rocky» pasó a «Cotizado».");
      expect(JSON.parse(String(posts(api)[0]![1]?.body))).toEqual({ stage_id: "s2", version: 1 });
    });

    it("con el teclado, Escape o salir del asa cancelan, y soltar sin elegir etapa no envía; se saltan las cerradas", async () => {
      const api = setup();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "" } });
      await screen.findByText("Cama ganada");
      fireEvent.keyDown(handle(), { key: "Enter" });
      fireEvent.keyDown(handle(), { key: "ArrowRight" });
      expect(await said("Etapa «Cotizado»: suéltala aquí.")).toBeInTheDocument();
      fireEvent.keyDown(handle(), { key: "ArrowRight" }); // «Ganado» y «Perdido» no reciben
      expect(await said("No hay más etapas en esa dirección.")).toBeInTheDocument();
      fireEvent.keyDown(handle(), { key: "Escape" });
      expect(handle()).toHaveAttribute("aria-pressed", "false");
      fireEvent.keyDown(handle(), { key: " " });
      fireEvent.keyDown(handle(), { key: " " }); // sin etapa elegida
      expect(
        await said("Movimiento cancelado: «Collar para Rocky» sigue en «Nuevo»."),
      ).toBeInTheDocument();
      fireEvent.keyDown(handle(), { key: " " });
      fireEvent.blur(handle());
      expect(handle()).toHaveAttribute("aria-pressed", "false");
      expect(posts(api)).toHaveLength(0);
    });

    it("un rechazo vuelve la tarjeta a su sitio con el mensaje del movimiento", async () => {
      setup({ status: 409, body: { code: "STALE_VERSION" } });
      await grab();
      under("Cotizado");
      move();
      fireEvent.pointerUp(handle(), { pointerId: 1 });
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Otra persona cambió esta oportunidad antes que tú. La tarjeta volvió a su etapa.",
      );
      expect(within(column("Nuevo")).getByText("Collar para Rocky")).toBeVisible();
    });

    it("el asa se ignora con una orden en curso, las cerradas no la tienen y el asa es táctil-segura", async () => {
      let release = () => {};
      const held = new Promise<void>((resolve) => (release = resolve));
      const api = setup();
      const send = api.getMockImplementation()!;
      api.mockImplementation(async (input, init) => {
        if (init?.method === "POST") await held;
        return send(input, init);
      });
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      expect(handle()).toHaveClass("touch-none"); // el dedo sobre el asa no desplaza la página
      expect(handle()).toHaveAccessibleDescription(/Espacio o Intro para tomarla/);
      fireEvent.change(screen.getByRole("combobox", { name: /Mover «Collar para Rocky»/ }), {
        target: { value: "s2" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Mover «Collar para Rocky»" }));
      await waitFor(() => expect(handle()).toHaveAttribute("aria-disabled", "true"));
      fireEvent.pointerDown(handle(), {
        button: 0,
        isPrimary: true,
        pointerId: 1,
        clientX: 1,
        clientY: 1,
      });
      expect(handle()).toHaveAttribute("aria-pressed", "false"); // no la tomó
      fireEvent.keyDown(handle(), { key: " " });
      expect(screen.queryByText(/tomada/)).toBeNull();
      release();
      await screen.findByText("«Collar para Rocky» pasó a «Cotizado».");
      expect(posts(api)).toHaveLength(1);
      fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "" } });
      await screen.findByText("Cama ganada");
      expect(screen.queryByRole("button", { name: /Arrastrar «Cama ganada»/ })).toBeNull();
    });

    const pressed = (value: boolean) =>
      expect(handle()).toHaveAttribute("aria-pressed", String(value));
    const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

    it("solo el dedo principal: otro dedo, otro botón o Ctrl+clic no mueven ni sueltan", async () => {
      const api = setup();
      await grab();
      under("Cotizado");
      move();
      await said("Etapa «Cotizado»: suéltala aquí.");
      under(null);
      // Un segundo dedo en la misma asa: ni se toma, ni mueve la elección, ni suelta.
      fireEvent.pointerDown(handle(), {
        button: 0,
        isPrimary: false,
        pointerId: 2,
        clientX: 5,
        clientY: 5,
      });
      fireEvent.pointerMove(handle(), { pointerId: 2, clientX: 200, clientY: 200 });
      fireEvent.pointerUp(handle(), { pointerId: 2, clientX: 200, clientY: 200 });
      fireEvent.pointerCancel(handle(), { pointerId: 2 });
      expect(column("Cotizado")).toHaveClass("bg-accent/30");
      pressed(true);
      under("Cotizado");
      fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 80, clientY: 80 });
      await screen.findByText("«Collar para Rocky» pasó a «Cotizado».");
      expect(posts(api)).toHaveLength(1);
    });

    it("el botón secundario, Ctrl+clic y un pointercancel no arrastran", async () => {
      const api = setup();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      for (const init of [{ button: 2 }, { button: 0, ctrlKey: true }, { isPrimary: false }]) {
        fireEvent.pointerDown(handle(), {
          isPrimary: true,
          pointerId: 1,
          clientX: 1,
          clientY: 1,
          ...init,
        });
        pressed(false);
      }
      await grab();
      under("Cotizado");
      move();
      fireEvent.pointerCancel(handle(), { pointerId: 1 });
      expect(
        await said("Movimiento cancelado: «Collar para Rocky» sigue en «Nuevo»."),
      ).toBeInTheDocument();
      pressed(false);
      // Perder la captura (la asa se desmonta, el sistema la quita) también suelta sin enviar.
      await grab();
      under("Cotizado");
      move();
      fireEvent.lostPointerCapture(handle(), { pointerId: 1 });
      pressed(false);
      await settle();
      expect(posts(api)).toHaveLength(0);
    });

    it("lo que hay bajo el puntero al levantarlo decide: salir de la columna y soltar fuera no envía", async () => {
      const api = setup();
      await grab();
      under("Cotizado");
      move();
      await said("Etapa «Cotizado»: suéltala aquí.");
      under(null); // se fue sin un movimiento más
      fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 400, clientY: 400 });
      expect(
        await said("Movimiento cancelado: «Collar para Rocky» sigue en «Nuevo»."),
      ).toBeInTheDocument();
      await settle();
      expect(posts(api)).toHaveLength(0);
    });

    it("una etapa inactiva no recibe la tarjeta, y las que sí, se marcan con borde discontinuo", async () => {
      const api = setup();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "" } });
      await screen.findByText("Cama vieja");
      fireEvent.pointerDown(handle(), {
        button: 0,
        isPrimary: true,
        pointerId: 1,
        clientX: 1,
        clientY: 1,
      });
      expect(column("Cotizado")).toHaveClass("border-dashed");
      for (const closed of ["Nuevo", "Antigua", "Ganado"])
        expect(column(closed)).not.toHaveClass("border-dashed");
      under("Antigua");
      move();
      fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 80, clientY: 80 });
      await said("Movimiento cancelado: «Collar para Rocky» sigue en «Nuevo».");
      await settle();
      expect(posts(api)).toHaveLength(0);
    });

    it("una tecla mantenida no suelta, y Alt+flecha o una flecha sin tarjeta tomada no se bloquean", async () => {
      setup();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      expect(fireEvent.keyDown(handle(), { key: "ArrowLeft" })).toBe(true); // nada tomado: del navegador
      fireEvent.keyDown(handle(), { key: " ", repeat: true });
      pressed(false);
      fireEvent.keyDown(handle(), { key: " " });
      pressed(true);
      fireEvent.keyDown(handle(), { key: "Enter", repeat: true }); // no suelta
      pressed(true);
      for (const modifier of ["altKey", "metaKey", "ctrlKey", "shiftKey"]) {
        expect(fireEvent.keyDown(handle(), { key: "ArrowRight", [modifier]: true })).toBe(true);
      }
      expect(screen.queryByText(/Etapa «Cotizado»/)).toBeNull();
    });

    it("otra asa o el puntero no sueltan una tarjeta tomada con el teclado, y viceversa", async () => {
      const api = setup();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.keyDown(handle(), { key: " " });
      fireEvent.keyDown(handle(), { key: "ArrowRight" });
      const other = handle("Cama Luna");
      fireEvent.pointerDown(other, {
        button: 0,
        isPrimary: true,
        pointerId: 1,
        clientX: 1,
        clientY: 1,
      });
      fireEvent.pointerUp(other, { pointerId: 1, clientX: 1, clientY: 1 });
      fireEvent.keyDown(other, { key: " " });
      pressed(true); // sigue tomada la primera
      await settle();
      expect(posts(api)).toHaveLength(0);
      fireEvent.keyDown(handle(), { key: "Escape" });
      fireEvent.pointerDown(handle(), {
        button: 0,
        isPrimary: true,
        pointerId: 1,
        clientX: 1,
        clientY: 1,
      });
      fireEvent.keyDown(handle(), { key: " " }); // el teclado no suelta lo del puntero
      pressed(true);
    });

    it("si la tarjeta tomada pierde su asa (se pasa a la tabla) no queda tomada para siempre", async () => {
      setup();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.keyDown(handle(), { key: " " });
      fireEvent.click(screen.getByRole("button", { name: "Tabla" })); // sin mover el foco
      await waitFor(() => expect(screen.queryByRole("button", { name: /^Arrastrar/ })).toBeNull());
      fireEvent.click(screen.getByRole("button", { name: "Tablero" }));
      pressed(false);
      fireEvent.keyDown(handle(), { key: " " });
      expect(await screen.findByText(/tomada, en «Nuevo»/)).toBeInTheDocument();
      pressed(true);
    });

    it("al desmontar se quitan los oyentes de teclado", async () => {
      const remove = vi.spyOn(window, "removeEventListener");
      setup();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.keyDown(handle(), { key: " " });
      cleanup();
      expect(remove.mock.calls.some(([type]) => type === "keydown")).toBe(true);
      remove.mockRestore();
    });
  });
});
