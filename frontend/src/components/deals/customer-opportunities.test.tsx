import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Opportunity, Pipeline, SelfContext, Stage } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { CustomerOpportunities } from "./customer-opportunities";

const CONTACT = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const LIST = `GET /api/v1/o/acme/opportunities/?contact_id=${CONTACT}&limit=200`;
const PIPELINES = "GET /api/v1/o/acme/pipelines/?limit=200";
const MEMBERS = "GET /api/v1/o/acme/members/?limit=200";
const CREATE = "POST /api/v1/o/acme/opportunities/";
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
const stage = (id: string, name: string, changes: Partial<Stage> = {}): Stage => ({
  ...{ id, name, code: id, sort_order: 0, stage_type: "OPEN", default_probability: 10 },
  ...{ stale_after_days: null, color: "#0ea5e9", requires_fields: {}, is_active: true },
  ...changes,
});
const pipeline = (id: string, name: string, changes: Partial<Pipeline> = {}): Pipeline => ({
  ...{ id, name, code: id, kind: "RETAIL", currency: "PEN", is_default: false, is_active: true },
  ...{ sort_order: 0, version: 1, stages: [stage("s1", "Nuevo")] },
  ...changes,
});
const sales = pipeline("p1", "Ventas", { is_default: true });
const opportunity = (id: string, changes: Partial<Opportunity> = {}): Opportunity => ({
  ...{ id, number: `OPP-00000${id}`, title: "Collar para Rocky", pipeline_id: "p1" },
  ...{ contact: { id: CONTACT, display_name: "Rosa Díaz" } },
  ...{ stage: { id: "s1", name: "Nuevo", type: "OPEN" }, status: "OPEN" },
  ...{ amount: "1500.50", currency: "PEN", probability: 10, priority: "MEDIUM" },
  ...{ assigned_user_id: "u1", next_action: "Llamar el viernes", expected_close_date: null },
  ...{ stage_entered_at: "2026-10-30T10:00:00Z", last_activity_at: "2026-10-30T10:00:00Z" },
  ...{ won_at: null, lost_at: null, lost_reason: null, version: 1 },
  ...changes,
});
const rocky = opportunity("1");
const luna = opportunity("3", {
  ...{ title: "Cama Luna", amount: "300.25", assigned_user_id: "u2", next_action: null },
  ...{
    stage: { id: "s2", name: "Cotizado", type: "OPEN" },
    stage_entered_at: "2026-10-20T10:00:00Z",
  },
});
const won = opportunity("4", {
  ...{ title: "Venta ganada", status: "WON", stage: { id: "s4", name: "Ganado", type: "WON" } },
  ...{ amount: "80", currency: "USD" },
});
const lost = opportunity("2", {
  ...{ title: "Venta perdida", status: "LOST", stage: { id: "s5", name: "Perdido", type: "LOST" } },
  lost_reason: { id: "r1", name: "Sin stock" },
});

const show = (codes: string[] = ["opportunities.view"]) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions: grants(...codes) }}>
      <CustomerOpportunities contactId={CONTACT} contactName="Rosa Díaz" />
    </TenantProvider>,
  );
const titles = () =>
  within(screen.getByRole("list", { name: "Oportunidades" }))
    .getAllByRole("listitem")
    .map((row) => row.querySelector("span")!.textContent);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-02T10:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("CustomerOpportunities (F5-06)", () => {
  it("pide solo las del cliente, abiertas primero y las más nuevas antes, con todos sus datos", async () => {
    const api = mockApi({ [PIPELINES]: page([sales]), [LIST]: page([rocky, lost, luna, won]) });
    show();
    expect(await screen.findByText("Cama Luna")).toBeVisible();
    expect(titles()).toEqual(["Cama Luna", "Collar para Rocky", "Venta ganada", "Venta perdida"]);
    const [cama, collar, ganada, perdida] = screen.getAllByRole("listitem");
    expect(collar).toHaveTextContent("Collar para Rocky");
    expect(collar).toHaveTextContent("OPP-000001");
    expect(collar).toHaveTextContent("Abierta");
    expect(collar).toHaveTextContent("Etapa: Nuevo");
    expect(collar).toHaveTextContent("S/ 1,500.50");
    expect(collar).toHaveTextContent("Yo");
    expect(collar).toHaveTextContent("3 días en la etapa");
    expect(collar).toHaveTextContent("Próxima acción: Llamar el viernes");
    expect(cama).toHaveTextContent("Etapa: Cotizado");
    expect(cama).toHaveTextContent("Otro miembro"); // sin `users.view` no se nombra
    expect(cama).toHaveTextContent("13 días en la etapa");
    expect(cama).toHaveTextContent("Sin próxima acción");
    expect(ganada).toHaveTextContent("Ganada");
    expect(ganada).toHaveTextContent("USD 80.00"); // la moneda es la de la oportunidad
    expect(perdida).toHaveTextContent("Perdida: Sin stock");
    expect(collar).not.toHaveTextContent("Rosa Díaz"); // el cliente es el de la ficha
    // 390 px: una columna, sin tabla ni desplazamiento horizontal propio; el texto largo se parte.
    expect(collar).toHaveClass("flex-col");
    expect(within(collar!).getByText("Collar para Rocky")).toHaveClass("wrap-anywhere");
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull(); // sin pantalla de detalle todavía
    expect(api.mock.calls.map(([url]) => String(url)).sort()).toEqual(
      [PIPELINES, LIST].map((route) => route.slice(4)).sort(),
    );
  });

  it("con `users.view` nombra al responsable con el directorio", async () => {
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
    show(["opportunities.view", "users.view"]);
    expect(await screen.findByText("Luis Ramos")).toBeVisible();
  });

  it("carga, vacío, fallo con reintento y 403", async () => {
    let reply: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    mockApi({ [PIPELINES]: page([sales]), [LIST]: () => reply });
    show();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando las oportunidades del cliente…");
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    reply = page([]);
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("Este cliente todavía no tiene oportunidades.")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("un 403 se explica y no se reintenta; la API decide quién las ve", async () => {
    mockApi({
      [PIPELINES]: { status: 403, body: { code: "PERMISSION_DENIED" } },
      [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } },
    });
    show(["opportunities.view", "opportunities.create"]);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver las oportunidades de esta organización.",
    );
    expect(screen.queryByRole("button", { name: /Reintentar|Nueva oportunidad/ })).toBeNull();
  });

  it("«Cargar más» sigue el cursor con el mismo cliente y avisa de que el orden es de lo cargado", async () => {
    const api = mockApi({
      [PIPELINES]: page([sales]),
      [LIST]: page([rocky], "tras-1"),
      [`${LIST}&cursor=tras-1`]: page([luna]),
    });
    show();
    expect(await screen.findByText("Collar para Rocky")).toBeVisible();
    expect(screen.getByText(/se ordenan solo entre las cargadas/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("Cama Luna")).toBeVisible();
    expect(titles()).toEqual(["Cama Luna", "Collar para Rocky"]);
    expect(screen.queryByRole("button", { name: "Cargar más" })).toBeNull();
    expect(String(api.mock.calls.at(-1)![0])).toContain("&cursor=tras-1");
  });

  describe("crear desde la ficha", () => {
    const fresh = opportunity("9", {
      title: "Collar nuevo",
      amount: "99.50",
      next_action: "Llamar",
    });
    const created = { ...fresh, items: [], history: [] };
    const posts = (api: ReturnType<typeof mockApi>) =>
      api.mock.calls.filter(([, init]) => init?.method === "POST");
    function setup(reply: { status: number; body?: unknown }, pipelines = [sales]) {
      const server = { rows: [rocky] as unknown[] };
      let release = () => {};
      const held = new Promise<void>((resolve) => (release = resolve));
      const api = mockApi({
        [PIPELINES]: page(pipelines),
        [LIST]: () => page(server.rows),
        [CREATE]: reply,
      });
      const send = api.getMockImplementation()!;
      api.mockImplementation(async (input, init) => {
        if (init?.method === "POST") await held;
        return send(input, init);
      });
      document.cookie = "csrftoken=token-de-prueba";
      show(["opportunities.view", "opportunities.create"]);
      return { api, server, release };
    }
    const open = async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Nueva oportunidad" }));
      return screen.getByRole("form", { name: "Nueva oportunidad" });
    };
    const fill = (label: string, value: string) =>
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    const submit = () => fireEvent.click(screen.getByRole("button", { name: "Crear oportunidad" }));

    it("el cliente ya está elegido: no hay buscador, y envía su `contact_id` y el pipeline predeterminado", async () => {
      const { api, server, release } = setup({ status: 201, body: created });
      await open();
      expect(screen.queryByRole("group", { name: "Cliente" })).toBeNull();
      expect(screen.queryByRole("searchbox")).toBeNull();
      fill("Título", "Collar nuevo");
      fill("Importe estimado (opcional)", "99.5");
      fill("Próxima acción (opcional)", "Llamar");
      submit();
      expect(screen.getByRole("button", { name: "Creando…" })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
      server.rows = [rocky, fresh];
      release();
      expect(await screen.findByText("«Collar nuevo» creada en «Nuevo».")).toBeVisible();
      expect(JSON.parse(String(posts(api)[0]![1]?.body))).toEqual({
        ...{ contact_id: CONTACT, title: "Collar nuevo", pipeline_id: "p1" },
        ...{ amount: "99.5", next_action: "Llamar" },
      });
      // Sin pedir clientes: no hay nada que buscar.
      expect(api.mock.calls.some(([url]) => String(url).includes("/contacts/"))).toBe(false);
      // La nueva aparece en la lista, entre las abiertas, y recibe el foco.
      expect(await screen.findByText("Collar nuevo")).toBeVisible();
      expect(screen.queryByRole("form", { name: "Nueva oportunidad" })).toBeNull();
      await waitFor(() => expect(document.getElementById("opportunity-9")).toHaveFocus());
      expect(screen.getByRole("status")).toHaveTextContent("«Collar nuevo» creada en «Nuevo».");
    });

    it("sin título o con un importe inválido no envía nada", async () => {
      const { api } = setup({ status: 201, body: created });
      await open();
      submit();
      expect(screen.getByText("Escribe el título de la oportunidad.")).toBeVisible();
      fill("Título", "Collar nuevo");
      fill("Importe estimado (opcional)", "1,5");
      submit();
      expect(screen.getByText(/El importe no es válido/)).toBeVisible();
      expect(posts(api)).toHaveLength(0);
    });

    it("dos envíos seguidos son una sola petición", async () => {
      const { api, release } = setup({ status: 201, body: created });
      const form = await open();
      fill("Título", "Collar nuevo");
      fireEvent.submit(form);
      fireEvent.submit(form);
      release();
      await screen.findByText(/creada en|se creó en/);
      expect(posts(api)).toHaveLength(1);
    });

    it("un 403 de la API se dice en el formulario, que conserva lo escrito", async () => {
      const { release } = setup({ status: 403, body: { code: "PERMISSION_DENIED" } });
      await open();
      fill("Título", "Collar nuevo");
      submit();
      release();
      expect(await screen.findByText("No tienes permiso para crear oportunidades.")).toBeVisible();
      expect(screen.getByLabelText("Título")).toHaveValue("Collar nuevo");
    });

    it("un cliente que la API rechaza se dice sin ofrecer elegir otro", async () => {
      const { release } = setup({
        status: 400,
        body: { code: "VALIDATION_ERROR", fields: { contact_id: ["No válido."] } },
      });
      await open();
      fill("Título", "Collar nuevo");
      submit();
      release();
      expect(await screen.findByText(/Ese cliente no existe o no está a tu alcance/)).toBeVisible();
    });

    it("con varios pipelines deja elegir y envía el elegido; con uno no ofrece la lista", async () => {
      const second = pipeline("p2", "Mayoristas", { sort_order: 1 });
      const { api, release } = setup({ status: 201, body: created }, [sales, second]);
      await open();
      const choice = screen.getByLabelText("Pipeline");
      expect(
        within(choice)
          .getAllByRole("option")
          .map((o) => o.textContent),
      ).toEqual(["Ventas", "Mayoristas"]);
      fireEvent.change(choice, { target: { value: "p2" } });
      fill("Título", "Collar nuevo");
      submit();
      release();
      await screen.findByText(/creada en|se creó en/);
      expect(JSON.parse(String(posts(api)[0]![1]?.body)).pipeline_id).toBe("p2");
    });

    it("un solo pipeline no muestra selector", async () => {
      setup({ status: 201, body: created });
      await open();
      expect(screen.queryByLabelText("Pipeline")).toBeNull();
    });

    it("sin una etapa abierta y activa el botón lo explica y no abre el formulario", async () => {
      setup({ status: 201, body: created }, [pipeline("p1", "Ventas", { stages: [] })]);
      const button = await screen.findByRole("button", { name: "Nueva oportunidad" });
      expect(button).toHaveAttribute("aria-disabled", "true");
      fireEvent.click(button);
      expect(screen.queryByRole("form", { name: "Nueva oportunidad" })).toBeNull();
      expect(screen.getByText(/no tiene una etapa abierta y activa/)).toBeVisible();
    });

    it("sin `opportunities.create` no se ofrece crear (la API decide igual)", async () => {
      mockApi({ [PIPELINES]: page([sales]), [LIST]: page([rocky]) });
      show();
      expect(await screen.findByText("Collar para Rocky")).toBeVisible();
      expect(screen.queryByRole("button", { name: "Nueva oportunidad" })).toBeNull();
    });
  });
});
