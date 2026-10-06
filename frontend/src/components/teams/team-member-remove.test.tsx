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
const REMOVE = (id: string) => `DELETE /api/v1/o/acme/teams/t2/members/${id}/`;
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
const person = (id: string, email: string, first = "", last = ""): TeamMember => ({
  id,
  status: "ACTIVE",
  team_role: "MEMBER",
  is_active: true,
  user: { id: `u-${id}`, email, first_name: first, last_name: last },
});
const ana = person("m1", "ana@acme.pe", "Ana", "López"); // quien usa la pantalla
const luis = person("m2", "luis@acme.pe", "Luis", "Paz");
const bare = person("m6", "sin-nombre@acme.pe");
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const NO_CONTENT = { status: 204, body: undefined };
const routes = (extra: Parameters<typeof mockApi>[0] = {}) => ({
  [LIST]: page([sales]),
  [MEMBERS]: page([ana, luis, bare]),
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
const LUIS = "Quitar a Luis Paz (luis@acme.pe) del equipo Ventas";
// Abre la confirmación de una fila, como una pulsación real: el botón tiene el foco.
function ask(panel: HTMLElement, name = LUIS) {
  const button = within(panel).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
  return within(panel).getByRole("group", { name });
}
const names = (panel: HTMLElement) =>
  within(within(panel).getByRole("list", { name: "Integrantes de Ventas" }))
    .getAllByRole("listitem")
    .map((row) => row.querySelector("span")!.textContent);

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("TeamMemberRemove", () => {
  it("se ofrece a quien administra equipos, en cada integrante menos en uno mismo", async () => {
    const api = mockApi(routes());
    // Ver equipos y personas, o un rol «owner», no bastan: decide el permiso que dio la API.
    const reader = tenant(["teams.view", "users.view"], [{ code: "owner", name: "Owner" }]);
    const view = renderApp(ui(reader));
    await panelOf();
    expect(screen.queryByRole("button", { name: /Quitar/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    const panel = await panelOf();
    const offered = within(panel).getAllByRole("button", { name: /^Quitar a / });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      LUIS, // con el correo: dos personas pueden llamarse igual
      "Quitar a sin-nombre@acme.pe del equipo Ventas", // sin nombre: su correo, una vez
    ]); // y no «Ana López»: nadie se quita a sí mismo
    expect(offered.map((button) => button.textContent)).toEqual(["Quitar", "Quitar"]);
    await tick();
    expect(calls(api, "DELETE")).toHaveLength(0);
  });

  it("pide confirmación en la fila, dice la consecuencia y cancelar no envía nada", async () => {
    const api = mockApi(routes());
    renderApp(ui());
    const panel = await panelOf();
    const group = ask(panel);
    expect(group).toHaveTextContent(
      "¿Quitar a Luis Paz del equipo Ventas? Seguirá siendo miembro de la organización.",
    );
    expect(group).toHaveAccessibleDescription(/Seguirá siendo miembro de la organización/);
    expect(group.parentElement).toHaveClass("w-full"); // abierta ocupa su fila
    const cancel = within(group).getByRole("button", { name: "Cancelar" });
    expect(cancel).toHaveFocus(); // la opción que no cambia nada
    fireEvent.click(cancel);
    expect(within(panel).getByRole("button", { name: LUIS })).toHaveFocus();
    expect(within(panel).queryByRole("group", { name: LUIS })).not.toBeInTheDocument();
    await tick();
    expect(calls(api, "DELETE")).toHaveLength(0);
    expect(names(panel)).toEqual(["Ana López", "Luis Paz", "sin-nombre@acme.pe"]);
  });

  it("al confirmar envía una vez, la fila deja el panel sin volver a pedirlo y se anuncia", async () => {
    const api = mockApi(routes({ [REMOVE("m2")]: NO_CONTENT }));
    renderApp(ui());
    const panel = await panelOf();
    const live = within(panel).getAllByRole("status")[1]!; // montada antes de tener texto
    expect(live).toHaveTextContent("");
    const group = ask(panel);
    const release = hold(api);
    const confirm = within(group).getByRole("button", { name: "Sí, quitar" });
    confirm.focus();
    fireEvent.click(confirm);
    fireEvent.click(confirm); // ocupado: la segunda pulsación no cuenta
    const cancel = within(group).getByRole("button", { name: "Cancelar" });
    fireEvent.click(cancel); // ya se envió: no se cierra
    await waitFor(() => expect(confirm).toHaveTextContent("Quitando…"));
    for (const button of [confirm, cancel]) expect(button).toHaveAttribute("aria-disabled", "true");
    // Un dedo impaciente: pulsa de nuevo en cuanto la pantalla enseña un botón libre.
    const watch = new MutationObserver(() => {
      const again = within(panel).queryByRole("button", { name: /^(Sí, quitar|Quitar a Luis)/ });
      if (again?.getAttribute("aria-disabled") !== "true") again?.click();
    });
    watch.observe(panel, { subtree: true, childList: true, attributes: true, characterData: true });
    release();
    await waitFor(() => expect(names(panel)).toEqual(["Ana López", "sin-nombre@acme.pe"]));
    await tick();
    watch.disconnect();
    expect(calls(api, "DELETE")).toHaveLength(1);
    expect(String(calls(api, "DELETE")[0]![0])).toBe("/api/v1/o/acme/teams/t2/members/m2/");
    expect(within(panel).getByText("2 integrantes")).toBeVisible();
    expect(live).toHaveTextContent("Luis Paz ya no está en el equipo Ventas.");
    expect(live).not.toHaveClass("sr-only"); // se ve
    expect(within(panel).getByRole("button", { name: "Cerrar" })).toHaveFocus(); // la fila se fue
    expect(asked(api, "/api/v1/o/acme/teams/t2/members/?")).toBe(1); // el panel no se vuelve a pedir
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([
    [403, "PERMISSION_DENIED", "No tienes permiso para cambiar los integrantes de los equipos."],
    [500, "INTERNAL_ERROR", "Algo salió mal de nuestro lado."],
  ])(
    "un %i %s se explica en la fila, no la quita y el mismo botón reintenta",
    async (status, code, text) => {
      let reply: { status: number; body: unknown } = {
        status,
        body: { code, message: "texto de la API" },
      };
      const api = mockApi(routes({ [REMOVE("m2")]: () => reply }));
      renderApp(ui());
      const panel = await panelOf();
      const group = ask(panel);
      const confirm = within(group).getByRole("button", { name: "Sí, quitar" });
      fireEvent.click(confirm);
      expect(await within(group).findByRole("alert")).toHaveTextContent(text);
      expect(group).not.toHaveTextContent("texto de la API");
      expect(names(panel)).toHaveLength(3);
      fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
      expect(within(ask(panel)).queryByRole("alert")).not.toBeInTheDocument(); // al reabrir, sin el error
      reply = NO_CONTENT;
      fireEvent.click(within(panel).getByRole("button", { name: "Sí, quitar" }));
      await waitFor(() => expect(names(panel)).toHaveLength(2));
      expect(calls(api, "DELETE")).toHaveLength(2);
    },
  );

  it("sin red el intento falla y se dice; sin sesión va al login una vez y sigue ocupado", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/equipos", search: "", assign });
    const api = mockApi(
      routes({ [REMOVE("m2")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } }),
    );
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const group = ask(await panelOf());
    const confirm = within(group).getByRole("button", { name: "Sí, quitar" });
    onlineManager.setOnline(false); // una escritura no espera en cola a que vuelva la red
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(confirm);
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    fireEvent.click(confirm);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fequipos"));
    await tick();
    fireEvent.click(confirm); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(calls(api, "DELETE")).toHaveLength(2); // el que no salió y el 401
    expect(within(group).queryByRole("alert")).not.toBeInTheDocument();
    expect(confirm).toHaveTextContent("Quitando…");
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("un 404 cierra el panel, lo explica la lista con la persona y el equipo, y la vuelve a pedir", async () => {
    const api = mockApi(
      routes({ [REMOVE("m2")]: { status: 404, body: { code: "NOT_FOUND", message: "API" } } }),
    );
    renderApp(ui());
    const group = ask(await panelOf());
    const confirm = within(group).getByRole("button", { name: "Sí, quitar" });
    confirm.focus(); // el foco, dentro de la tarjeta
    fireEvent.click(confirm);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No se pudo quitar a Luis Paz del equipo Ventas: ya no estaba en él, o el equipo ya no está disponible. Revisa la lista.",
    );
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // ni la confirmación ni el panel
    await waitFor(() => expect(asked(api, "/api/v1/o/acme/teams/")).toBeGreaterThanOrEqual(3));
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    expect(calls(api, "DELETE")).toHaveLength(1);
  });

  it("si el panel se cierra y se reabre con el envío en vuelo, la respuesta no devuelve a quien se quitó", async () => {
    let members = [ana, luis];
    const api = mockApi(routes({ [MEMBERS]: () => page(members), [REMOVE("m2")]: NO_CONTENT }));
    renderApp(ui());
    const panel = await panelOf();
    const answer = hold(api);
    fireEvent.click(within(ask(panel)).getByRole("button", { name: "Sí, quitar" }));
    await waitFor(() => expect(within(panel).getByText("Quitando…")).toBeVisible());
    fireEvent.click(within(panel).getByRole("button", { name: "Cerrar" }));
    await tick(); // la lectura del panel cerrado ya se olvidó
    const read = hold(api); // la del panel reabierto, que sale antes de que responda el envío
    fireEvent.click(screen.getByRole("button", { name: OPEN }));
    await tick();
    members = [ana]; // lo que la API dirá a partir de ahora
    answer();
    await tick();
    members = [ana, luis]; // la lectura en vuelo salió antes del envío: trae la lista de antes
    read(); // llega tarde: ya no cuenta
    await tick();
    const again = screen.getByRole("group", { name: PANEL });
    expect(await within(again).findByText("1 integrante")).toBeVisible();
    expect(names(again)).toEqual(["Ana López"]);
    expect(calls(api, "DELETE")).toHaveLength(1);
  });

  it("al quitar al último lo dice, y quien salió vuelve a ser candidato para incorporar", async () => {
    const directory = [ana, luis].map((member) => ({ ...member, joined_at: "", roles: [] }));
    mockApi(
      routes({
        [MEMBERS]: page([luis]),
        [REMOVE("m2")]: NO_CONTENT,
        "GET /api/v1/o/acme/members/?limit=200": page(directory),
      }),
    );
    renderApp(ui());
    const panel = await panelOf();
    fireEvent.click(within(ask(panel)).getByRole("button", { name: "Sí, quitar" }));
    expect(
      await within(panel).findByText("Este equipo todavía no tiene integrantes."),
    ).toBeVisible();
    expect(within(panel).queryByRole("list")).not.toBeInTheDocument();
    fireEvent.click(within(panel).getByRole("button", { name: /^Incorporar integrantes/ }));
    const add = "Incorporar a Luis Paz (luis@acme.pe) al equipo Ventas";
    expect(await within(panel).findByRole("button", { name: add })).toBeVisible();
  });
});
