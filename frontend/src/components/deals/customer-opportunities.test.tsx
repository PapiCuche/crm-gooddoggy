import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Opportunity, SelfContext } from "@/lib/api/model";
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
// Los pipelines solo necesitan lo que la pantalla lee de ellos.
const stage = (id: string, name: string) => ({ id, name, stage_type: "OPEN", is_active: true });
const pipeline = (id: string, name: string, stages = [stage("s1", "Nuevo")]) => ({
  ...{ id, name, is_default: id === "p1", is_active: true, sort_order: 0, stages },
});
const sales = pipeline("p1", "Ventas");
const opportunity = (id: string, changes: Partial<Opportunity> = {}) =>
  ({
    ...{ id, number: `OPP-00000${id}`, title: "Collar para Rocky", status: "OPEN" },
    ...{ contact: { id: CONTACT, display_name: "Rosa Díaz" } },
    ...{ stage: { id: "s1", name: "Nuevo", type: "OPEN" }, amount: "1500.50", currency: "PEN" },
    ...{ assigned_user_id: "u1", next_action: "Llamar el viernes", lost_reason: null },
    ...{ stage_entered_at: "2026-10-30T10:00:00Z", ...changes },
  }) as unknown as Opportunity;
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
    // Título, número, estado, etapa, importe, responsable, días en la etapa y próxima acción.
    expect(collar).toHaveTextContent(
      "Collar para RockyOPP-000001AbiertaEtapa: NuevoS/ 1,500.50Yo3 días en la etapaPróxima acción: Llamar el viernes",
    );
    // Sin `users.view` no se nombra al responsable.
    expect(cama).toHaveTextContent(
      "Cama LunaOPP-000003AbiertaEtapa: CotizadoS/ 300.25Otro miembro13 días en la etapaSin próxima acción",
    );
    expect(ganada).toHaveTextContent("GanadaEtapa: GanadoUSD 80.00"); // la moneda es la de la oportunidad
    expect(perdida).toHaveTextContent("Perdida: Sin stock");
    expect(collar).not.toHaveTextContent("Rosa Díaz"); // el cliente es el de la ficha
    // 390 px: una columna, sin tabla ni desplazamiento horizontal propio; el texto largo se parte.
    expect(collar).toHaveClass("flex-col");
    expect(within(collar!).getByText("Collar para Rocky")).toHaveClass("wrap-anywhere");
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull(); // sin pantalla de detalle todavía
    expect(api).toHaveBeenCalledTimes(2); // pipelines y la lista del cliente; sin directorio
  });

  it("con `users.view` nombra al responsable con el directorio", async () => {
    mockApi({
      [PIPELINES]: page([sales]),
      [LIST]: page([luna]),
      [MEMBERS]: page([
        { user: { id: "u2", email: "l@acme.pe", first_name: "Luis", last_name: "Ramos" } },
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
    expect(
      await screen.findByText("Este cliente no tiene oportunidades que puedas ver."),
    ).toBeVisible();
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

  it("al cambiar de cliente no queda a la vista la lista del anterior", async () => {
    const OTHER = "7a2d3b5f-1c8e-4d9f-8b62-4e3a8f9c0d21";
    mockApi({
      [PIPELINES]: page([sales]),
      [LIST]: page([rocky]),
      [`GET /api/v1/o/acme/opportunities/?contact_id=${OTHER}&limit=200`]: page([]),
    });
    function Harness() {
      const [id, setId] = useState(CONTACT);
      return (
        <TenantProvider value={tenant}>
          <button onClick={() => setId(OTHER)}>Otro cliente</button>
          <CustomerOpportunities contactId={id} contactName="Otro" />
        </TenantProvider>
      );
    }
    renderApp(<Harness />);
    expect(await screen.findByText("Collar para Rocky")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Otro cliente" }));
    expect(
      await screen.findByText("Este cliente no tiene oportunidades que puedas ver."),
    ).toBeVisible();
    expect(screen.queryByText("Collar para Rocky")).toBeNull();
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
      const form = await open();
      expect(screen.queryByLabelText("Pipeline")).toBeNull(); // con uno solo no hay selector
      expect(screen.queryByRole("group", { name: "Cliente" })).toBeNull();
      expect(screen.queryByRole("searchbox")).toBeNull();
      fill("Título", "Collar nuevo");
      fill("Importe estimado (opcional)", "99.5");
      fill("Próxima acción (opcional)", "Llamar");
      submit();
      fireEvent.submit(form); // dos envíos seguidos son una sola petición
      expect(screen.getByRole("button", { name: "Creando…" })).toHaveAttribute(
        "aria-disabled",
        "true",
      );
      server.rows = [rocky, fresh];
      release();
      expect(await screen.findByText("«Collar nuevo» creada en «Nuevo».")).toBeVisible();
      expect(posts(api)).toHaveLength(1);
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

    it.each([
      [403, { code: "PERMISSION_DENIED" }, "No tienes permiso para crear oportunidades."],
      [
        400,
        { code: "VALIDATION_ERROR", fields: { contact_id: ["No válido."] } },
        "Ese cliente ya no existe o no está a tu alcance.", // no hay a quién elegir
      ],
    ])(
      "un %i de la API se dice en el formulario, que conserva lo escrito",
      async (status, body, text) => {
        const { release } = setup({ status, body });
        await open();
        fill("Título", "Collar nuevo");
        submit();
        release();
        const message = await screen.findByText(text);
        expect(message).not.toHaveTextContent("Elige otro");
        expect(screen.getByLabelText("Título")).toHaveValue("Collar nuevo");
      },
    );

    it("con varios pipelines deja elegir y envía el elegido", async () => {
      const second = pipeline("p2", "Mayoristas");
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

    it("un pipeline que la API rechaza no manda elegir otro si solo hay uno, y sí si hay varios", async () => {
      const bad = {
        status: 400,
        body: { code: "VALIDATION_ERROR", fields: { pipeline_id: ["No válido."] } },
      };
      const one = setup(bad);
      await open();
      fill("Título", "Collar nuevo");
      submit();
      one.release();
      const text = await screen.findByText(/Este pipeline ya no está disponible/);
      expect(text).not.toHaveTextContent("Elige otro");
      cleanup();
      const many = setup(bad, [sales, pipeline("p2", "Mayoristas")]);
      await open();
      fill("Título", "Collar nuevo");
      submit();
      many.release();
      expect(await screen.findByText(/Elige otro/)).toBeVisible();
    });

    it("si la lista no la trae, lo dice sin hablar de filtros que la ficha no tiene", async () => {
      const { release } = setup({ status: 201, body: created }); // el servidor sigue sin listarla
      await open();
      fill("Título", "Collar nuevo");
      submit();
      release();
      const text = await screen.findByText(/pero la lista no la muestra/);
      expect(text).not.toHaveTextContent("filtros");
    });

    it("sin una etapa abierta y activa el botón lo explica y no abre el formulario", async () => {
      setup({ status: 201, body: created }, [pipeline("p1", "Ventas", [])]);
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
