import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Member, SelfContext, Team, TeamMember } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { TeamsList } from "./teams-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/teams/";
const MEMBERS = "GET /api/v1/o/acme/teams/t2/members/?limit=200";
const PEOPLE = "GET /api/v1/o/acme/members/?limit=200";
const ADD = (id: string) => `PUT /api/v1/o/acme/teams/t2/members/${id}/`;
const ALL = ["teams.view", "users.view", "teams.manage"];
const tenant = (codes: string[] = ALL, roles: SelfContext["roles"] = []): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles,
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const sales: Team = {
  id: "t2", // distinto del `slug`: la ruta lleva el `id`
  slug: "ventas",
  name: "Ventas",
  description: "",
  assignment_strategy: "MANUAL",
  is_active: true,
};
const user = (id: string, email: string, first = "", last = "") => ({
  id: `u-${id}`,
  email,
  first_name: first,
  last_name: last,
});
const person = (id: string, email: string, first = "", last = "", status = "ACTIVE"): Member => ({
  id,
  status: status as Member["status"],
  joined_at: "2026-01-01T00:00:00Z",
  user: user(id, email, first, last),
  roles: [],
});
const inTeam = (from: Member, role: TeamMember["team_role"] = "MEMBER"): TeamMember => ({
  id: from.id,
  status: from.status,
  team_role: role,
  is_active: true,
  user: from.user,
});
const ana = person("m1", "ana@acme.pe", "Ana", "López"); // quien usa la pantalla
const luis = person("m2", "luis@acme.pe", "Luis", "Paz");
const marta = person("m3", "marta@acme.pe", "Marta", "Ríos");
const eva = person("m4", "eva@acme.pe", "Eva", "", "SUSPENDED");
const gone = person("m5", "baja@acme.pe", "Baja", "", "DEACTIVATED");
const bare = person("m6", "sin-nombre@acme.pe");
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
// La lista de equipos, los integrantes de Ventas (Luis) y el directorio de la organización.
const routes = (extra: Parameters<typeof mockApi>[0] = {}) => ({
  [LIST]: page([sales]),
  [MEMBERS]: page([inTeam(luis, "SUPERVISOR")]),
  [PEOPLE]: page([ana, luis, marta, eva, gone, bare]),
  ...extra,
});
const ui = (context = tenant()) => (
  <TenantProvider value={context}>
    <TeamsList />
  </TenantProvider>
);
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const asked = (api: ReturnType<typeof mockApi>, path: string) =>
  api.mock.calls.filter(([url]) => String(url).startsWith(path)).length;
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
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
// El panel de integrantes de Ventas, ya con su respuesta.
async function panelOf() {
  const name = "Ver los integrantes del equipo Ventas (ventas)";
  fireEvent.click(await screen.findByRole("button", { name }));
  const panel = screen.getByRole("group", { name: "Integrantes del equipo Ventas (ventas)" });
  await within(panel).findByText(/^\d+ integrantes?$/); // el recuento: ya respondió
  return panel;
}
const opener = () =>
  screen.getByRole("button", { name: "Incorporar integrantes al equipo Ventas (ventas)" });
// La lista de candidatos, ya con su respuesta.
async function picker() {
  const panel = await panelOf();
  fireEvent.click(opener());
  const group = screen.getByRole("group", { name: "Incorporar a Ventas" });
  await waitFor(() => expect(within(group).queryByText("Cargando miembros…")).toBeNull());
  return { panel, group };
}
const offered = (group: HTMLElement) =>
  within(group)
    .queryAllByRole("button", { name: /^Incorporar a / })
    .map((button) => button.getAttribute("aria-label"));
const adder = (group: HTMLElement, name: string) =>
  within(group).getByRole("button", { name: `Incorporar a ${name} al equipo Ventas` });
const team = (panel: HTMLElement) =>
  within(within(panel).getByRole("list", { name: "Integrantes de Ventas" }))
    .getAllByRole("listitem")
    .map((row) => [...row.querySelectorAll("span")].map((span) => span.textContent).join(" | "));

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("TeamMemberAdd", () => {
  it("solo se ofrece a quien administra equipos, y el directorio no se pide hasta abrir la lista", async () => {
    const api = mockApi(routes());
    // Ver equipos y personas, o un rol «owner», no bastan: decide el permiso que dio la API.
    const reader = tenant(["teams.view", "users.view"], [{ code: "owner", name: "Owner" }]);
    const view = renderApp(ui(reader));
    await panelOf();
    expect(screen.queryByRole("button", { name: /Incorporar/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    await panelOf();
    expect(opener()).toHaveTextContent("Incorporar integrante");
    await tick();
    expect(asked(api, "/api/v1/o/acme/members/")).toBe(0);
    expect(calls(api, "PUT")).toHaveLength(0);
  });

  it("lista a quien aún no está: ni quien ya está, ni una baja, ni uno mismo; y sigue el cursor", async () => {
    const api = mockApi(
      routes({
        [PEOPLE]: page([ana, luis, marta], "abc"),
        [`${PEOPLE}&cursor=abc`]: page([eva, gone, bare]),
      }),
    );
    renderApp(ui());
    const panel = await panelOf();
    const release = hold(api);
    opener().focus(); // como una pulsación real: el botón tiene el foco y desaparece
    fireEvent.click(opener());
    const group = screen.getByRole("group", { name: "Incorporar a Ventas" });
    expect(within(group).getAllByRole("status")[0]).toHaveTextContent("Cargando miembros…");
    const done = within(group).getByRole("button", { name: "Listo" });
    await waitFor(() => expect(done).toHaveFocus()); // «Incorporar integrante» se fue
    release();
    await waitFor(() => expect(offered(group)).toHaveLength(3));
    expect(offered(group)).toEqual([
      "Incorporar a Marta Ríos al equipo Ventas",
      "Incorporar a Eva al equipo Ventas", // suspendida: la API la admite
      "Incorporar a sin-nombre@acme.pe al equipo Ventas", // sin nombre: su correo
    ]);
    expect(group).toHaveTextContent(
      "No apareces en la lista: nadie se incorpora a sí mismo a un equipo.",
    );
    expect(panel).toContainElement(group); // dentro del panel del equipo
    expect(asked(api, "/api/v1/o/acme/members/")).toBe(2);
    expect(calls(api, "PUT")).toHaveLength(0); // abrir no escribe
  });

  it("incorpora con un solo envío, lo enseña en el panel con lo que respondió la API y lo anuncia", async () => {
    // La API responde con el integrante; aquí, con un papel que nadie pidió: se enseña el suyo.
    const saved = inTeam(marta, "SUPERVISOR");
    const api = mockApi(routes({ [ADD("m3")]: { status: 201, body: saved } }));
    renderApp(ui());
    const { panel, group } = await picker();
    const live = within(group).getAllByRole("status").at(-1)!; // montada antes de tener texto
    expect(live).toHaveTextContent("");
    const button = adder(group, "Marta Ríos");
    const release = hold(api);
    button.focus();
    fireEvent.click(button);
    fireEvent.click(button); // ocupado: la segunda pulsación no cuenta
    fireEvent.click(adder(group, "Eva")); // ni otra persona mientras tanto
    fireEvent.click(within(group).getByRole("button", { name: "Listo" })); // ni se cierra
    await waitFor(() => expect(button).toHaveTextContent("Incorporando…"));
    expect(adder(group, "Eva")).toHaveAttribute("aria-disabled", "true");
    release();
    await waitFor(() => expect(offered(group)).toHaveLength(2)); // ya no es candidata
    expect(calls(api, "PUT")).toHaveLength(1);
    const [url, init] = calls(api, "PUT")[0]!;
    expect([String(url), (init as RequestInit).body]).toEqual([
      "/api/v1/o/acme/teams/t2/members/m3/", // el `id` del equipo y el de la membresía
      "{}", // entra con lo que pone la API
    ]);
    expect(team(panel)).toEqual([
      "Luis Paz | luis@acme.pe | Supervisor",
      "Marta Ríos | marta@acme.pe | Supervisor", // al final, con el papel de la respuesta
    ]);
    expect(within(panel).getByText("2 integrantes")).toBeVisible();
    expect(live).toHaveTextContent("Marta Ríos se incorporó al equipo Ventas.");
    expect(live).not.toHaveClass("sr-only"); // se ve
    await waitFor(() => expect(within(group).getByRole("button", { name: "Listo" })).toHaveFocus());
    expect(asked(api, "/api/v1/o/acme/teams/t2/members/?")).toBe(1); // el panel no se vuelve a pedir
    fireEvent.click(adder(group, "Eva")); // otra: el anuncio anterior se retira al enviar
    expect(live).toHaveTextContent("");
  });

  it.each([
    [403, "PERMISSION_DENIED", "No tienes permiso para cambiar los integrantes de los equipos."],
    [400, "VALIDATION_ERROR", "Algo salió mal de nuestro lado."], // no hay campos que revisar
    [500, "INTERNAL_ERROR", "Algo salió mal de nuestro lado."],
  ])(
    "un %i %s se explica, no cambia el panel y el mismo botón reintenta",
    async (status, code, text) => {
      let reply = { status, body: { code, message: "texto de la API" } as unknown };
      const api = mockApi(routes({ [ADD("m3")]: () => reply }));
      renderApp(ui());
      const { panel, group } = await picker();
      fireEvent.click(adder(group, "Marta Ríos"));
      expect(await within(group).findByRole("alert")).toHaveTextContent(text);
      expect(group).not.toHaveTextContent("texto de la API");
      expect(team(panel)).toHaveLength(1);
      expect(offered(group)).toHaveLength(3);
      reply = { status: 201, body: inTeam(marta) };
      fireEvent.click(adder(group, "Marta Ríos"));
      await waitFor(() => expect(team(panel)).toHaveLength(2));
      expect(within(group).queryByRole("alert")).not.toBeInTheDocument();
      expect(calls(api, "PUT")).toHaveLength(2);
    },
  );

  it("sin red el intento falla y se dice; sin sesión va al login una vez y sigue ocupado", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/equipos", search: "", assign });
    const api = mockApi(
      routes({ [ADD("m3")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } }),
    );
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const { group } = await picker();
    onlineManager.setOnline(false); // una escritura no espera en cola a que vuelva la red
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(adder(group, "Eva"));
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    fireEvent.click(adder(group, "Marta Ríos"));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fequipos"));
    await tick();
    fireEvent.click(adder(group, "Marta Ríos")); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(group).getByRole("button", { name: "Listo" }));
    await tick();
    expect(calls(api, "PUT")).toHaveLength(2); // el que no salió y el 401
    expect(within(group).queryByRole("alert")).not.toBeInTheDocument();
    expect(adder(group, "Marta Ríos")).toHaveTextContent("Incorporando…");
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("un 404 cierra el panel, lo explica la lista con la persona y el equipo, y la vuelve a pedir", async () => {
    const api = mockApi(
      routes({ [ADD("m3")]: { status: 404, body: { code: "NOT_FOUND", message: "API" } } }),
    );
    renderApp(ui());
    const { group } = await picker();
    const button = adder(group, "Marta Ríos");
    button.focus(); // el foco, dentro de la tarjeta
    fireEvent.click(button);
    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent(
      "No se pudo incorporar a Marta Ríos al equipo Ventas: el equipo o la persona ya no están disponibles. Revisa la lista.",
    );
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // ni el panel ni la lista
    await waitFor(() => expect(asked(api, "/api/v1/o/acme/teams/")).toBeGreaterThanOrEqual(3));
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    expect(calls(api, "PUT")).toHaveLength(1);
  });

  it("«Listo» cierra la lista y devuelve el foco; reabrir vuelve a preguntar; sin candidatos lo dice", async () => {
    let people = [ana, luis, marta];
    const api = mockApi(
      routes({
        [MEMBERS]: page([inTeam(luis), inTeam(ana)]), // quien usa la pantalla ya está
        [PEOPLE]: () => page(people),
      }),
    );
    renderApp(ui());
    const { panel, group } = await picker();
    expect(offered(group)).toEqual(["Incorporar a Marta Ríos al equipo Ventas"]);
    expect(group).not.toHaveTextContent("No apareces en la lista"); // ya está: no hace falta
    const done = within(group).getByRole("button", { name: "Listo" });
    done.focus();
    fireEvent.click(done);
    expect(screen.queryByRole("group", { name: "Incorporar a Ventas" })).not.toBeInTheDocument();
    expect(opener()).toHaveFocus();
    expect(panel).toBeVisible(); // el panel sigue abierto
    people = [ana, luis, gone]; // ya no queda nadie
    fireEvent.click(opener());
    const again = screen.getByRole("group", { name: "Incorporar a Ventas" });
    expect(await within(again).findByText("No queda nadie por incorporar.")).toBeVisible();
    expect(offered(again)).toEqual([]);
    expect(asked(api, "/api/v1/o/acme/members/")).toBe(2);
    expect(calls(api, "PUT")).toHaveLength(0);
  });

  it("si el directorio falla se explica y se puede reintentar", async () => {
    let reply: { status: number; body: unknown } = { status: 500, body: { code: "X" } };
    const api = mockApi(routes({ [PEOPLE]: () => reply }));
    renderApp(ui());
    await panelOf();
    fireEvent.click(opener());
    const group = screen.getByRole("group", { name: "Incorporar a Ventas" });
    expect(await within(group).findByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(offered(group)).toEqual([]);
    reply = page([ana, luis, marta]);
    fireEvent.click(within(group).getByRole("button", { name: "Reintentar" }));
    await waitFor(() => expect(offered(group)).toHaveLength(1));
    expect(asked(api, "/api/v1/o/acme/members/")).toBe(3); // el fallo, su reintento y el del botón
  });
});
