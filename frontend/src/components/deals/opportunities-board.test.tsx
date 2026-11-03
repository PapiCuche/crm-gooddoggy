import { fireEvent, screen, waitFor, within } from "@testing-library/react";
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
      [409, "OPPORTUNITY_CLOSED", "Esta oportunidad ya está cerrada: no se mueve."],
      [409, "MISSING_FIELDS", "La etapa pide datos que faltan o no está disponible."],
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

    it("una oportunidad ganada no tiene control; las abiertas, sí", async () => {
      const won = opportunity("4", {
        ...{ title: "Cama ganada", status: "WON" },
        stage: { id: "s4", name: "Ganado", type: "WON" },
      });
      mockApi({
        [PIPELINES]: page([sales]),
        [LIST]: page([rocky]),
        "GET /api/v1/o/acme/opportunities/?pipeline_id=p1": page([rocky, won]),
      });
      show();
      await screen.findByRole("region", { name: "Tablero de oportunidades" });
      fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "" } });
      expect(await screen.findByText("Cama ganada")).toBeVisible();
      expect(screen.queryByRole("combobox", { name: /Cama ganada/ })).toBeNull();
      expect(control("Collar")).toBeVisible();
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
});
