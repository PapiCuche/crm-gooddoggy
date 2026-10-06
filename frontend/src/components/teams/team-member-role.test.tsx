import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Team, TeamMember } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { TeamsList } from "./teams-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/teams/";
const MEMBERS = "GET /api/v1/o/acme/teams/t2/members/?limit=200";
const ROLE = (id: string) => `PUT /api/v1/o/acme/teams/t2/members/${id}/`;
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
const person = (id: string, email: string, first = "", last = "", role = "MEMBER"): TeamMember => ({
  id,
  status: "ACTIVE",
  team_role: role as TeamMember["team_role"],
  is_active: true,
  user: { id: `u-${id}`, email, first_name: first, last_name: last },
});
const ana = person("m1", "ana@acme.pe", "Ana", "López"); // quien usa la pantalla
const luis = person("m2", "luis@acme.pe", "Luis", "Paz");
const marta = person("m3", "marta@acme.pe", "Marta", "Ríos", "SUPERVISOR");
const odd = person("m6", "raro@acme.pe", "", "", "LEAD"); // un papel que esta versión no conoce
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const routes = (extra: Parameters<typeof mockApi>[0] = {}) => ({
  [LIST]: page([sales]),
  [MEMBERS]: page([ana, luis, marta, odd]),
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
const OPEN = "Ver los integrantes del equipo Ventas (ventas)";
const PANEL = "Integrantes del equipo Ventas (ventas)";
// El panel de integrantes de Ventas, ya con su respuesta.
async function panelOf() {
  fireEvent.click(await screen.findByRole("button", { name: OPEN }));
  const panel = screen.getByRole("group", { name: PANEL });
  await within(panel).findByText(/^\d+ integrantes?$/);
  return panel;
}
const UP = "Hacer supervisor a Luis Paz (luis@acme.pe) en el equipo Ventas";
const DOWN = "Hacer integrante a Luis Paz (luis@acme.pe) en el equipo Ventas";
const row = (panel: HTMLElement, name: string) =>
  within(panel).getByText(name).closest("li") as HTMLElement;
// Lo que la fila enseña de la persona: nombre, correo y papel.
const shown = (panel: HTMLElement, name: string) =>
  [...row(panel, name).querySelectorAll("p:not([role]) > span")].map((span) => span.textContent);
// La región de la acción, en la fila: montada desde el principio.
const said = (panel: HTMLElement, name: string) =>
  within(row(panel, name)).getByRole("status").textContent;
// Como una pulsación real: el botón tiene el foco.
function press(panel: HTMLElement, name: string) {
  const button = within(panel).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
  return button;
}
const answer = (from: TeamMember, role: string, extra: Partial<TeamMember> = {}) => ({
  status: 200,
  body: { ...from, team_role: role, ...extra },
});

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("TeamMemberRole", () => {
  it("se ofrece a quien administra equipos: lo contrario del papel de cada uno, menos en uno mismo", async () => {
    const api = mockApi(routes());
    // Ver equipos y personas, o un rol «owner», no bastan: decide el permiso que dio la API.
    const reader = tenant(["teams.view", "users.view"], [{ code: "owner", name: "Owner" }]);
    const view = renderApp(ui(reader));
    await panelOf();
    expect(screen.queryByRole("button", { name: /^Hacer / })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    const panel = await panelOf();
    const offered = within(panel).getAllByRole("button", { name: /^Hacer / });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      UP, // con el correo: dos personas pueden llamarse igual
      "Hacer integrante a Marta Ríos (marta@acme.pe) en el equipo Ventas",
    ]); // ni «Ana López» (nadie cambia lo suyo) ni quien tiene un papel desconocido
    expect(offered.map((button) => button.textContent)).toEqual([
      "Hacer supervisor",
      "Hacer integrante",
    ]);
    expect(within(row(panel, "raro@acme.pe")).getByRole("button")).toHaveTextContent("Quitar");
    await tick();
    expect(calls(api, "PUT")).toHaveLength(0);
  });

  it("envía una vez, la fila enseña el papel que respondió la API sin volver a pedir el panel y se anuncia", async () => {
    const api = mockApi(routes({ [ROLE("m2")]: answer(luis, "SUPERVISOR") }));
    renderApp(ui());
    const panel = await panelOf();
    expect(said(panel, "Luis Paz")).toBe(""); // montada antes de tener texto
    fireEvent.click(within(panel).getByRole("button", { name: UP }), { detail: 2 });
    await tick();
    expect(calls(api, "PUT")).toHaveLength(0); // el segundo clic de un doble clic no cuenta
    const release = hold(api);
    const button = press(panel, UP);
    fireEvent.click(button); // ocupado: la segunda pulsación no cuenta
    await waitFor(() => expect(button).toHaveTextContent("Guardando…"));
    expect(button).toHaveAttribute("aria-disabled", "true");
    // Un dedo impaciente: pulsa de nuevo en cuanto la pantalla enseña ese botón libre.
    const watch = new MutationObserver(() => {
      const again = within(panel).queryByRole("button", { name: UP });
      if (again?.getAttribute("aria-disabled") === "false") again.click();
    });
    watch.observe(panel, { subtree: true, childList: true, attributes: true, characterData: true });
    release();
    await within(panel).findByRole("button", { name: DOWN }); // pasa a la acción contraria
    await tick();
    watch.disconnect();
    expect(calls(api, "PUT")).toHaveLength(1);
    const [url, init] = calls(api, "PUT")[0]!;
    expect([String(url), (init as RequestInit).body]).toEqual([
      "/api/v1/o/acme/teams/t2/members/m2/", // el `id` del equipo y el de la membresía
      JSON.stringify({ team_role: "SUPERVISOR" }), // y nada más
    ]);
    expect(shown(panel, "Luis Paz")).toEqual(["Luis Paz", "luis@acme.pe", "Supervisor"]);
    expect(said(panel, "Luis Paz")).toBe("Luis Paz ahora es supervisor del equipo Ventas.");
    expect(within(panel).getByRole("button", { name: DOWN })).toHaveFocus(); // el mismo botón
    expect(shown(panel, "Marta Ríos")).toEqual(["Marta Ríos", "marta@acme.pe", "Supervisor"]);
    expect(asked(api, "/api/v1/o/acme/teams/t2/members/?")).toBe(1); // el panel no se vuelve a pedir
    expect(within(panel).getByText("4 integrantes")).toBeVisible();
  });

  it("la fila enseña lo que respondió la API, no lo que se pidió; y el mismo botón lo deshace", async () => {
    // La API no cambió el papel, y dice además que la membresía está suspendida.
    let reply = answer(luis, "MEMBER", { status: "SUSPENDED" });
    const api = mockApi(routes({ [ROLE("m2")]: () => reply }));
    renderApp(ui());
    const panel = await panelOf();
    press(panel, UP);
    await waitFor(() => expect(said(panel, "Luis Paz")).toMatch(/ahora es integrante/));
    expect(shown(panel, "Luis Paz")).toEqual([
      "Luis Paz",
      "luis@acme.pe",
      "Integrante",
      "Membresía: Suspendido", // la fila entera, como la respondió la API
    ]);
    reply = answer(luis, "SUPERVISOR");
    press(panel, UP);
    await within(panel).findByRole("button", { name: DOWN });
    reply = answer(luis, "MEMBER");
    press(panel, DOWN);
    await within(panel).findByRole("button", { name: UP });
    expect(said(panel, "Luis Paz")).toBe("Luis Paz ahora es integrante del equipo Ventas.");
    expect(calls(api, "PUT").map(([, init]) => (init as RequestInit).body)).toEqual([
      '{"team_role":"SUPERVISOR"}',
      '{"team_role":"SUPERVISOR"}',
      '{"team_role":"MEMBER"}',
    ]);
  });

  it.each([
    [403, "PERMISSION_DENIED", "No tienes permiso para cambiar los integrantes de los equipos."],
    [400, "VALIDATION_ERROR", "Algo salió mal de nuestro lado."], // no hay campos que revisar
    [500, "INTERNAL_ERROR", "Algo salió mal de nuestro lado."],
  ])(
    "un %i %s se explica en la fila, no la cambia y el mismo botón reintenta",
    async (status, code, text) => {
      let reply: { status: number; body: unknown } = {
        status,
        body: { code, message: "texto de la API" },
      };
      const api = mockApi(routes({ [ROLE("m2")]: () => reply }));
      renderApp(ui());
      const panel = await panelOf();
      press(panel, UP);
      expect(await within(row(panel, "Luis Paz")).findByRole("alert")).toHaveTextContent(text);
      expect(panel).not.toHaveTextContent("texto de la API");
      expect(shown(panel, "Luis Paz")).toEqual(["Luis Paz", "luis@acme.pe", "Integrante"]);
      expect(said(panel, "Luis Paz")).toBe("");
      reply = answer(luis, "SUPERVISOR");
      press(panel, UP);
      await within(panel).findByRole("button", { name: DOWN });
      expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
      expect(calls(api, "PUT")).toHaveLength(2);
    },
  );

  it("sin red el intento falla y se dice; sin sesión va al login una vez y sigue ocupado", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/equipos", search: "", assign });
    const api = mockApi(
      routes({ [ROLE("m2")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } }),
    );
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const panel = await panelOf();
    onlineManager.setOnline(false); // una escritura no espera en cola a que vuelva la red
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const button = press(panel, UP);
    expect(await within(panel).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    fireEvent.click(button);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fequipos"));
    await tick();
    fireEvent.click(button); // sigue ocupado hasta que cambia la página
    await tick();
    expect(calls(api, "PUT")).toHaveLength(2); // el que no salió y el 401
    expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
    expect(button).toHaveTextContent("Guardando…");
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("un 404 cierra el panel, lo explica la lista con la persona y el equipo, y la vuelve a pedir", async () => {
    const api = mockApi(
      routes({ [ROLE("m2")]: { status: 404, body: { code: "NOT_FOUND", message: "API" } } }),
    );
    renderApp(ui());
    press(await panelOf(), UP); // el foco, dentro de la tarjeta
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No se pudo cambiar el papel de Luis Paz en el equipo Ventas: el equipo o la persona ya no están disponibles. Revisa la lista.",
    );
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    const lists = () => api.mock.calls.filter(([url]) => String(url) === "/api/v1/o/acme/teams/");
    await waitFor(() => expect(lists()).toHaveLength(2)); // la lista de equipos, otra vez
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    expect(calls(api, "PUT")).toHaveLength(1);
  });

  it("si el panel se cierra y se reabre con el envío en vuelo, una lectura atrasada no devuelve el papel de antes", async () => {
    let members = [ana, luis];
    const api = mockApi(
      routes({ [MEMBERS]: () => page(members), [ROLE("m2")]: answer(luis, "SUPERVISOR") }),
    );
    renderApp(ui());
    const panel = await panelOf();
    const respond = hold(api);
    const button = press(panel, UP);
    await waitFor(() => expect(button).toHaveTextContent("Guardando…"));
    fireEvent.click(within(panel).getByRole("button", { name: "Cerrar" }));
    await tick(); // la lectura del panel cerrado ya se olvidó
    const read = hold(api); // la del panel reabierto, que sale antes de que responda el envío
    fireEvent.click(screen.getByRole("button", { name: OPEN }));
    await tick();
    members = [ana, { ...luis, team_role: "SUPERVISOR" }]; // lo que la API dirá a partir de ahora
    respond();
    await tick();
    members = [ana, luis]; // la lectura en vuelo salió antes del envío: trae el papel de antes
    read(); // llega tarde: ya no cuenta
    await tick();
    const again = screen.getByRole("group", { name: PANEL });
    await within(again).findByRole("button", { name: DOWN });
    await tick();
    expect(shown(again, "Luis Paz")).toEqual(["Luis Paz", "luis@acme.pe", "Supervisor"]);
    expect(calls(api, "PUT")).toHaveLength(1);
  });
});
