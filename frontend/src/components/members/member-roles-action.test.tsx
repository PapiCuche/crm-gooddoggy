import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Member, Role, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { MembersList } from "./members-list";

const LIST = "GET /api/v1/o/acme/members/";
const ROLES = "GET /api/v1/o/acme/roles/?limit=200";
const link = (method: string, member: string, role: string) =>
  `${method} /api/v1/o/acme/members/${member}/roles/${role}/`;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "ana",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const role = (id: string, name: string): Role => ({
  id,
  code: id,
  name,
  description: "",
  is_system: false,
  permissions: [],
  members: 0,
});
const sales = role("sales", "Ventas");
const cash = role("cash", "Caja");
const member = (id: string, roles: Role[] = [], status: Member["status"] = "ACTIVE"): Member => ({
  id,
  status,
  joined_at: "2026-10-04T15:49:34Z",
  user: { id: `u-${id}`, email: `${id}@acme.pe`, first_name: "", last_name: "" },
  roles: roles.map(({ id: roleId, code, name }) => ({ id: roleId, code, name })),
});
const list = (results: Member[]) => ({ status: 200, body: { results, next: null } });
const everyone = () => list([member("ana"), member("luis", [cash]), member("eva", [], "INVITED")]);
const all = { status: 200, body: { results: [sales, cash], next: null } };
const ui = (context = tenant("users.view", "users.manage", "roles.view")) => (
  <TenantProvider value={context}>
    <MembersList />
  </TenantProvider>
);
const row = (email: string) =>
  screen.getByText(email, { selector: ".font-medium" }).closest("li") as HTMLElement;
const shown = (email: string) =>
  within(within(row(email)).getByRole("list", { name: "Roles" }))
    .getAllByRole("listitem")
    .map((item) => item.textContent);
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => init?.method === method).map(([url]) => String(url));
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

async function panel(name = "luis@acme.pe") {
  fireEvent.click(await screen.findByRole("button", { name: `Cambiar los roles de ${name}` }));
  const group = screen.getByRole("group", { name: `Roles de ${name}` });
  await within(group).findAllByRole("listitem");
  return group;
}

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con el token ya entregado: la escritura no lo pide antes
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("MemberRolesAction", () => {
  it("se ofrece con los dos permisos, a otros miembros, y no pide nada hasta abrirse", async () => {
    const api = mockApi({ [LIST]: everyone(), [ROLES]: all });
    const view = renderApp(ui(tenant("users.view", "users.manage")));
    await screen.findByRole("list", { name: "Miembros" });
    expect(screen.queryByRole("button", { name: /Cambiar los roles/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui(tenant("users.view", "roles.view"))); // ver roles no es administrarlos
    await screen.findByRole("list", { name: "Miembros" });
    expect(screen.queryByRole("button", { name: /Cambiar los roles/ })).not.toBeInTheDocument();
    renderApp(ui());
    const offered = await screen.findAllByRole("button", { name: /Cambiar los roles/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Cambiar los roles de luis@acme.pe", // ni la fila propia; el estado del miembro no importa
      "Cambiar los roles de eva@acme.pe",
    ]);
    expect(calls(api, "GET")).not.toContain("/api/v1/o/acme/roles/?limit=200");
    const group = await panel();
    expect(within(group).getByRole("button", { name: "Cerrar" })).toHaveFocus();
    const offers = within(group)
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label"));
    expect(offers).toEqual([null, "Asignar Ventas a luis@acme.pe", "Quitar Caja a luis@acme.pe"]);
    fireEvent.click(within(group).getByRole("button", { name: "Cerrar" }));
    expect(screen.getByRole("button", { name: "Cambiar los roles de luis@acme.pe" })).toHaveFocus();
    expect([...calls(api, "PUT"), ...calls(api, "DELETE")]).toEqual([]); // abrir no envía nada
  });

  it("asigna y quita de uno en uno, cambia la fila sin volver a pedir la lista y lo anuncia", async () => {
    const api = mockApi({
      [LIST]: list([member("ana"), member("luis", [sales]), member("eva")]),
      [ROLES]: all,
      [link("PUT", "luis", "cash")]: { status: 204 },
      [link("DELETE", "luis", "sales")]: { status: 204 },
    });
    renderApp(ui());
    const group = await panel();
    const release = hold(api);
    const assign = within(group).getByRole("button", { name: "Asignar Caja a luis@acme.pe" });
    const other = within(group).getByRole("button", { name: "Quitar Ventas a luis@acme.pe" });
    assign.focus();
    fireEvent.click(assign);
    fireEvent.click(assign); // ocupado: la segunda pulsación no cuenta
    fireEvent.click(other);
    await waitFor(() => expect(assign).toHaveTextContent("Asignando…"));
    for (const button of within(group).getAllByRole("button"))
      expect(button).toHaveAttribute("aria-disabled", "true"); // un cambio cada vez
    expect(other).toHaveTextContent(/^Quitar$/); // solo el botón pulsado dice que está en ello
    fireEvent.click(within(group).getByRole("button", { name: "Cerrar" })); // ya se envió
    expect(screen.getByRole("group")).toBeInTheDocument();
    release();
    await waitFor(() => expect(assign).toHaveAccessibleName("Quitar Caja a luis@acme.pe"));
    expect(assign).toHaveFocus(); // el mismo botón, ahora con la acción contraria
    expect(shown("luis@acme.pe")).toEqual(["Caja", "Ventas"]); // por nombre, como el directorio
    const status = () => within(row("luis@acme.pe")).getAllByRole("status").at(-1);
    expect(status()).toHaveTextContent("Rol Caja asignado a luis@acme.pe.");
    fireEvent.click(other);
    await waitFor(() => expect(shown("luis@acme.pe")).toEqual(["Caja"]));
    expect(status()).toHaveTextContent("Rol Ventas quitado a luis@acme.pe.");
    expect(calls(api, "PUT")).toEqual(["/api/v1/o/acme/members/luis/roles/cash/"]);
    expect(calls(api, "DELETE")).toEqual(["/api/v1/o/acme/members/luis/roles/sales/"]);
    expect(calls(api, "GET").filter((url) => url.includes("/members/"))).toHaveLength(1);
    expect(shown("eva@acme.pe")).toEqual(["Sin rol"]); // solo cambia la fila de la respuesta
  });

  it.each([
    [403, "PERMISSION_DENIED", "No tienes permiso para asignar ni quitar este rol a este miembro."],
    [409, "LAST_OWNER", "Debe quedar al menos un Owner activo en la organización."],
    [500, "INTERNAL_ERROR", "Algo salió mal de nuestro lado."],
  ])(
    "un %i %s se explica, no cambia la fila y el mismo botón reintenta",
    async (status, code, text) => {
      let reply: { status: number; body?: unknown } = { status, body: { code } };
      const api = mockApi({
        [LIST]: everyone(),
        [ROLES]: all,
        [link("DELETE", "luis", "cash")]: () => reply,
      });
      renderApp(ui());
      const group = await panel();
      const remove = within(group).getByRole("button", { name: "Quitar Caja a luis@acme.pe" });
      fireEvent.click(remove);
      expect(await within(group).findByRole("alert")).toHaveTextContent(text);
      expect(shown("luis@acme.pe")).toEqual(["Caja"]);
      expect(remove).toHaveAccessibleName("Quitar Caja a luis@acme.pe");
      await tick(); // estos errores no vuelven a pedir la lista
      expect(calls(api, "GET").filter((url) => url.includes("/members/"))).toHaveLength(1);
      reply = { status: 204 };
      fireEvent.click(remove);
      await waitFor(() => expect(shown("luis@acme.pe")).toEqual(["Sin rol"]));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );

  it("si el miembro ya no tiene el rol lo explica la lista, que se vuelve a pedir", async () => {
    let rows = [member("ana"), member("luis", [cash]), member("eva")];
    const api = mockApi({
      [LIST]: () => list(rows),
      [ROLES]: all,
      [link("DELETE", "luis", "cash")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await panel();
    rows = [member("ana"), member("luis"), member("eva")]; // otro se lo quitó antes
    fireEvent.click(within(group).getByRole("button", { name: "Quitar Caja a luis@acme.pe" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El estado de luis@acme.pe ya había cambiado. Revisa la lista.",
    );
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // el panel se cierra
    await waitFor(() => expect(shown("luis@acme.pe")).toEqual(["Sin rol"]));
    expect(calls(api, "GET").filter((url) => url.includes("/members/"))).toHaveLength(2);
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    await panel("eva@acme.pe"); // el aviso se va con la siguiente acción
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lo que hace cada botón queda fijado al abrir, aunque la lista cambie debajo", async () => {
    let luis = member("luis");
    const api = mockApi({
      [LIST]: () => list([member("ana"), luis]),
      [ROLES]: all,
      [link("PUT", "luis", "sales")]: { status: 204 },
    });
    const view = renderApp(ui());
    const group = await panel();
    luis = member("luis", [sales]); // otro administrador se lo asignó con el panel abierto
    await act(async () => {
      await view.client.refetchQueries();
      await new Promise((resolve) => setTimeout(resolve));
    });
    expect(shown("luis@acme.pe")).toEqual(["Ventas"]); // la fila ya lo dice
    const assign = within(group).getByRole("button", { name: "Asignar Ventas a luis@acme.pe" });
    fireEvent.click(assign); // el botón no se dio la vuelta: asignar otra vez no cambia nada
    await waitFor(() => expect(assign).toHaveAccessibleName("Quitar Ventas a luis@acme.pe"));
    expect(calls(api, "PUT")).toHaveLength(1);
    expect(calls(api, "DELETE")).toHaveLength(0);
    expect(shown("luis@acme.pe")).toEqual(["Ventas"]);
  });

  it("una lectura en vuelo no pisa los roles que acaban de cambiar", async () => {
    let luis = member("luis");
    const api = mockApi({
      [LIST]: () => ({ status: 200, body: { results: [member("ana"), luis], next: "abc" } }),
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [member("eva")], next: null } },
      [ROLES]: all,
      [link("PUT", "luis", "sales")]: { status: 204 },
      [link("PUT", "luis", "cash")]: { status: 204 },
    });
    const members = () => calls(api, "GET").filter((url) => url.includes("/members/"));
    const view = renderApp(ui());
    const group = await panel();
    const more = screen.getByRole("button", { name: "Cargar más" });
    let release = hold(api); // «Cargar más» sale ahora, con la lista de antes de la escritura
    fireEvent.click(more);
    await waitFor(() => expect(members()).toHaveLength(2));
    fireEvent.click(within(group).getByRole("button", { name: "Asignar Ventas a luis@acme.pe" }));
    await within(group).findByRole("button", { name: "Quitar Ventas a luis@acme.pe" });
    release();
    await tick();
    expect(shown("luis@acme.pe")).toEqual(["Ventas"]); // esa lectura se canceló
    await waitFor(() => expect(more).toHaveAttribute("aria-disabled", "false"));
    // Si lo que estaba en vuelo era la lista entera, se repite después de la escritura.
    release = hold(api);
    act(() => void view.client.refetchQueries({ queryKey: ["/api/v1/o/acme/members/", "pages"] }));
    await waitFor(() => expect(members()).toHaveLength(3));
    luis = member("luis", [cash, sales]); // lo que la API tendrá tras la escritura
    fireEvent.click(within(group).getByRole("button", { name: "Asignar Caja a luis@acme.pe" }));
    await within(group).findByRole("button", { name: "Quitar Caja a luis@acme.pe" });
    release();
    await waitFor(() => expect(members()).toHaveLength(4));
    expect(shown("luis@acme.pe")).toEqual(["Caja", "Ventas"]);
  });

  it("los roles del panel: todas sus páginas, sin roles, y un fallo que se puede reintentar", async () => {
    let reply: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    const api = mockApi({
      [LIST]: everyone(),
      [ROLES]: () => reply,
      [`${ROLES}&cursor=abc`]: { status: 200, body: { results: [cash], next: null } },
    });
    renderApp(ui());
    fireEvent.click(
      await screen.findByRole("button", { name: "Cambiar los roles de luis@acme.pe" }),
    );
    const group = screen.getByRole("group", { name: "Roles de luis@acme.pe" });
    expect(within(group).getByText("Cargando roles…")).toBeVisible();
    expect(await within(group).findByRole("alert")).toHaveTextContent("Algo salió mal");
    reply = { status: 200, body: { results: [sales], next: "abc" } };
    fireEvent.click(within(group).getByRole("button", { name: "Reintentar" }));
    await within(group).findByRole("button", { name: "Quitar Caja a luis@acme.pe" }); // página 2
    expect(
      within(group).getByRole("button", { name: "Asignar Ventas a luis@acme.pe" }),
    ).toBeVisible();
    fireEvent.click(within(group).getByRole("button", { name: "Cerrar" }));
    reply = { status: 200, body: { results: [], next: null } };
    fireEvent.click(screen.getByRole("button", { name: "Cambiar los roles de luis@acme.pe" }));
    expect(await screen.findByText("Esta organización no tiene roles.")).toBeVisible();
    expect(calls(api, "GET").filter((url) => url.includes("cursor=abc"))).toHaveLength(1);
  });

  it("sin red lo dice; sin sesión no enseña un error, va al login una vez y no reenvía", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/miembros", search: "", assign });
    const api = mockApi({
      [LIST]: everyone(),
      [ROLES]: all,
      [link("PUT", "luis", "sales")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const group = await panel();
    onlineManager.setOnline(false); // una escritura no espera en cola a que vuelva la red
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(within(group).getByRole("button", { name: "Quitar Caja a luis@acme.pe" }));
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    const button = within(group).getByRole("button", { name: "Asignar Ventas a luis@acme.pe" });
    fireEvent.click(button);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fmiembros"));
    await tick();
    fireEvent.click(button); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(group).getByRole("button", { name: "Cerrar" }));
    await tick();
    expect(calls(api, "PUT")).toHaveLength(1);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("group")).toBeInTheDocument();
    expect(shown("luis@acme.pe")).toEqual(["Caja"]);
  });
});
