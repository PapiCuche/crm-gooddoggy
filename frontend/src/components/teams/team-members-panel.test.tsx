import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Team, TeamMember } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { TeamsList } from "./teams-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/teams/";
const MEMBERS = (id: string, cursor = "") =>
  `GET /api/v1/o/acme/teams/${id}/members/?limit=200${cursor && `&cursor=${cursor}`}`;
const tenant = (
  codes: string[] = ["teams.view", "users.view"],
  roles: SelfContext["roles"] = [],
): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles,
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const team = (id: string, slug: string, name: string): Team => ({
  id, // distinto del `slug`: la ruta lleva el `id`
  slug,
  name,
  description: "",
  assignment_strategy: "MANUAL",
  is_active: true,
});
const teams = [team("t1", "soporte", "Soporte"), team("t2", "ventas", "Ventas")];
const list = (results: Team[] = teams) => ({ status: 200, body: { results, next: null } });
const person = (id: string, email: string, extra: Partial<TeamMember> = {}): TeamMember => ({
  id,
  status: "ACTIVE",
  team_role: "MEMBER",
  is_active: true,
  user: { id: `u-${id}`, email, first_name: "", last_name: "" },
  ...extra,
});
const luis = person("m2", "luis@acme.pe", {
  team_role: "SUPERVISOR",
  user: { id: "u2", email: "luis@acme.pe", first_name: "Luis", last_name: "Paz" },
});
const page = (results: TeamMember[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const ui = (context = tenant()) => (
  <TenantProvider value={context}>
    <TeamsList />
  </TenantProvider>
);
const card = (name: string) =>
  screen.getByText(name, { selector: "span.font-medium" }).closest("li") as HTMLElement;
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const trigger = (name = "Ventas", slug = "ventas") =>
  screen.getByRole("button", { name: `Ver los integrantes del equipo ${name} (${slug})` });
// Las peticiones al panel de un equipo, en orden.
const asked = (api: ReturnType<typeof mockApi>, id = "t2") =>
  api.mock.calls.map(([url]) => String(url)).filter((url) => url.includes(`/teams/${id}/members/`));
// Cada integrante del panel, como lo lee el usuario: nombre, correo, papel y estado.
const rows = (panel: HTMLElement) =>
  within(within(panel).getByRole("list"))
    .getAllByRole("listitem")
    .map((row) => [...row.querySelectorAll("span")].map((span) => span.textContent));
// Deja en espera la siguiente respuesta de la API hasta que se llama a lo que devuelve.
function hold(api: ReturnType<typeof mockApi>) {
  const answer = api.getMockImplementation()!;
  let release = () => {};
  api.mockImplementationOnce(async (...request) => {
    await new Promise<void>((resolve) => (release = resolve));
    return answer(...request);
  });
  return () => release();
}
async function opened(name = "Ventas", slug = "ventas") {
  await screen.findByRole("list", { name: "Equipos" });
  fireEvent.click(trigger(name, slug));
  return screen.getByRole("group", { name: `Integrantes del equipo ${name} (${slug})` });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("TeamMembersPanel", () => {
  it("se ofrece a quien ve equipos y personas, y nada se pide hasta abrirlo", async () => {
    const api = mockApi({ [LIST]: list(), [MEMBERS("t2")]: page([luis]) });
    // Con un solo permiso de los dos, o con un rol «owner», no: decide lo que dio la API.
    for (const context of [
      tenant(["teams.view", "teams.manage"], [{ code: "owner", name: "Owner" }]),
      tenant(["teams.view"]),
    ]) {
      const view = renderApp(ui(context));
      await screen.findByRole("list", { name: "Equipos" });
      expect(screen.queryByRole("button", { name: /integrantes/ })).not.toBeInTheDocument();
      view.unmount();
    }
    renderApp(ui());
    await screen.findByRole("list", { name: "Equipos" });
    const offered = screen.getAllByRole("button", { name: /integrantes/ });
    expect(offered.map((button) => button.textContent)).toEqual(["Integrantes", "Integrantes"]);
    expect(screen.queryByRole("button", { name: /Editar|Desactivar/ })).not.toBeInTheDocument();
    await tick();
    expect(asked(api)).toEqual([]); // ni al montar la lista
    expect(document.body).toHaveFocus(); // ni toma el foco
  });

  it("enseña a cada integrante con su nombre, su correo, su papel y su estado si no está activo", async () => {
    const people = [
      luis,
      person("m3", "sin-nombre@acme.pe"), // sin nombre: el correo, una vez
      person("m4", "eva@acme.pe", {
        status: "SUSPENDED",
        is_active: false,
        user: { id: "u4", email: "eva@acme.pe", first_name: " Eva ", last_name: "" },
      }),
      person("m5", "raro@acme.pe", { status: "INVITED", team_role: "LEAD" as never }),
    ];
    const api = mockApi({ [LIST]: list(), [MEMBERS("t2")]: page(people) });
    renderApp(ui());
    const release = hold(api);
    const panel = await opened();
    expect(within(panel).getByRole("status")).toHaveTextContent("Cargando integrantes…");
    const close = within(panel).getByRole("button", { name: "Cerrar" });
    await waitFor(() => expect(close).toHaveFocus()); // el botón pulsado se fue
    release();
    expect(await within(panel).findByRole("list", { name: "Integrantes de Ventas" })).toBeVisible();
    expect(rows(panel)).toEqual([
      ["Luis Paz", "luis@acme.pe", "Supervisor"],
      ["sin-nombre@acme.pe", "Integrante"],
      ["Eva", "eva@acme.pe", "Integrante", "Suspendido"], // el estado en la organización
      ["raro@acme.pe", "LEAD", "Invitado"], // un papel que esta versión no conoce: su código
    ]);
    expect(within(panel).getByRole("status")).toHaveTextContent("4 integrantes");
    expect(panel).toHaveTextContent("Integrantes de Ventas");
    for (const text of ["Luis Paz", "luis@acme.pe"])
      expect(within(panel).getByText(text)).toHaveClass("wrap-anywhere"); // largo: se parte
    expect(asked(api)).toEqual(["/api/v1/o/acme/teams/t2/members/?limit=200"]); // el `id`
    expect(asked(api, "t1")).toEqual([]); // solo el equipo que se abrió
    expect(panel.closest("div.w-full")).not.toBeNull(); // abierto ocupa su fila
    expect(
      within(panel)
        .queryAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["Cerrar"]);
    expect(api.mock.calls.every(([, init]) => (init?.method ?? "GET") === "GET")).toBe(true);
  });

  it("pide todas las páginas, y un cursor que no avanza es un fallo, no una lista sin fin", async () => {
    const more = person("m3", "marta@acme.pe");
    let last: { status: number; body: unknown } = page([person("m4", "eva@acme.pe")]);
    const api = mockApi({
      [LIST]: list(),
      [MEMBERS("t2")]: page([luis], "abc"),
      [MEMBERS("t2", "abc")]: page([more], "def"),
      [MEMBERS("t2", "def")]: () => last,
    });
    renderApp(ui());
    let panel = await opened();
    await waitFor(() =>
      expect(within(panel).getByRole("status")).toHaveTextContent("3 integrantes"),
    );
    expect(rows(panel).map((row) => row[0])).toEqual(["Luis Paz", "marta@acme.pe", "eva@acme.pe"]);
    expect(asked(api)).toHaveLength(3);
    fireEvent.click(within(panel).getByRole("button", { name: "Cerrar" }));
    last = page([], "abc"); // la API repite un cursor
    panel = await opened();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("Algo salió mal");
    // Se detuvo: las tres peticiones y el reintento que hace toda lectura, no infinitas.
    expect(asked(api)).toHaveLength(9);
  });

  it("un equipo sin integrantes lo dice; cerrar devuelve el foco y reabrir vuelve a preguntar", async () => {
    let people: TeamMember[] = [];
    const api = mockApi({ [LIST]: list(), [MEMBERS("t2")]: () => page(people) });
    renderApp(ui());
    const panel = await opened();
    expect(
      await within(panel).findByText("Este equipo todavía no tiene integrantes."),
    ).toHaveAttribute("role", "status");
    expect(within(panel).queryByRole("list")).not.toBeInTheDocument(); // ni una lista vacía
    const close = within(panel).getByRole("button", { name: "Cerrar" });
    expect(fireEvent.keyDown(close, { key: "Enter", repeat: true })).toBe(false); // mantenido
    close.focus();
    fireEvent.click(close);
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    expect(fireEvent.keyDown(trigger(), { key: "Enter", repeat: true })).toBe(false);
    people = [luis]; // alguien lo incorporó mientras tanto
    const release = hold(api);
    const again = await opened();
    // Nada de la vez anterior mientras llega la respuesta nueva.
    expect(within(again).getByRole("status")).toHaveTextContent("Cargando integrantes…");
    release();
    await waitFor(() =>
      expect(within(again).getByRole("status")).toHaveTextContent("1 integrante"),
    );
    expect(within(again).getByRole("status").textContent).toBe("1 integrante"); // en singular
    expect(asked(api)).toHaveLength(2);
  });

  it("un fallo se explica y se puede reintentar; el foco pasa a «Cerrar» al llegar la lista", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR", message: "texto de la API" },
    };
    const api = mockApi({ [LIST]: list(), [MEMBERS("t2")]: () => reply });
    renderApp(ui());
    const panel = await opened();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(panel).not.toHaveTextContent("texto de la API");
    const retry = within(panel).getByRole("button", { name: "Reintentar" });
    retry.focus();
    const release = hold(api);
    fireEvent.click(retry);
    fireEvent.click(retry); // ocupado: la segunda pulsación no cuenta
    await waitFor(() => expect(retry).toHaveTextContent("Cargando integrantes…"));
    expect(retry).toHaveAttribute("aria-disabled", "true");
    expect(retry).toHaveFocus(); // no se desmonta mientras reintenta
    reply = page([luis]);
    release();
    await waitFor(() =>
      expect(within(panel).getByRole("status")).toHaveTextContent("1 integrante"),
    );
    expect(within(panel).getByRole("button", { name: "Cerrar" })).toHaveFocus();
    expect(asked(api)).toHaveLength(3); // el fallo, su reintento automático y el del botón
    expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("sin el permiso lo explica, sin reintento; sin red lo dice", async () => {
    const api = mockApi({
      [LIST]: list(),
      [MEMBERS("t2")]: { status: 403, body: { code: "PERMISSION_DENIED" } },
    });
    renderApp(ui());
    const panel = await opened();
    expect(await within(panel).findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver los integrantes de los equipos.",
    );
    expect(within(panel).queryByRole("button", { name: "Reintentar" })).not.toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: "Cerrar" }));
    const down = new TypeError("Failed to fetch");
    api.mockRejectedValueOnce(down).mockRejectedValueOnce(down); // el intento y su reintento
    const offline = await opened("Soporte", "soporte");
    expect(await within(offline).findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(within(offline).getByRole("button", { name: "Reintentar" })).toBeVisible();
  });

  it("sin sesión no enseña un error y va al login una vez", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/equipos", search: "", assign });
    mockApi({
      [LIST]: list(),
      [MEMBERS("t2")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const panel = await opened();
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fequipos"));
    await tick();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(within(panel).getByRole("status")).toHaveTextContent("Cargando integrantes…");
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("un 404 cierra el panel, lo explica la lista y la vuelve a pedir; abrir otro retira el aviso", async () => {
    let listed = teams;
    const api = mockApi({
      [LIST]: () => list(listed),
      [MEMBERS("t2")]: { status: 404, body: { code: "NOT_FOUND", message: "texto de la API" } },
      [MEMBERS("t1")]: page([luis]),
    });
    renderApp(ui());
    const release = hold(api);
    await opened();
    listed = [teams[0]!]; // lo que la API tiene de verdad
    (document.activeElement as HTMLElement).blur(); // el foco, en ninguna parte
    release();
    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent(
      "No se pudieron ver los integrantes del equipo Ventas: ya no está disponible. Revisa la lista.",
    );
    expect(notice).not.toHaveTextContent("texto de la API");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByText("Ventas", { selector: "span.font-medium" }),
      ).not.toBeInTheDocument(),
    );
    expect(api.mock.calls.filter(([url]) => String(url) === "/api/v1/o/acme/teams/")).toHaveLength(
      2,
    );
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    await opened("Soporte", "soporte"); // otra acción: el aviso anterior ya no aplica
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("un 404 con el equipo aún en la lista no mueve el foco de quien ya está en otra parte", async () => {
    const api = mockApi({
      [LIST]: list(),
      [MEMBERS("t2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const release = hold(api);
    await opened();
    const elsewhere = trigger("Soporte", "soporte");
    elsewhere.focus(); // en otra tarjeta
    release();
    expect(await screen.findByRole("alert")).toHaveTextContent("ya no está disponible");
    await tick();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    expect(trigger()).toBeVisible(); // la tarjeta sigue: la lista aún la trae
    expect(elsewhere).toHaveFocus();
  });

  it("los textos de la API se enseñan tal cual, y dos paneles abiertos no se mezclan", async () => {
    const odd = person("m9", "{count}@acme.pe", {
      team_role: "constructor" as never, // ni lo que hereda cualquier objeto
      status: "{x, plural}" as never,
      user: { id: "u9", email: "{count}@acme.pe", first_name: "<b>{team}</b>", last_name: "" },
    });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    mockApi({ [LIST]: list(), [MEMBERS("t2")]: page([odd]), [MEMBERS("t1")]: page([luis]) });
    renderApp(ui());
    const sales = await opened();
    const support = await opened("Soporte", "soporte");
    await within(sales).findByRole("list");
    await within(support).findByRole("list");
    expect(rows(sales)).toEqual([
      ["<b>{team}</b>", "{count}@acme.pe", "constructor", "{x, plural}"],
    ]);
    expect(rows(support)).toEqual([["Luis Paz", "luis@acme.pe", "Supervisor"]]);
    expect(card("Ventas")).toContainElement(sales);
    expect(card("Soporte")).toContainElement(support);
    expect(errors).not.toHaveBeenCalled(); // ningún texto sin resolver
  });
});
