import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Permission, Role, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { RolesList } from "./roles-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/roles/";
const CATALOG = "GET /api/v1/o/acme/permissions/";
const GRANT = (id: string, code: string) => `PUT /api/v1/o/acme/roles/${id}/permissions/${code}/`;
const REVOKE = (id: string, code: string) =>
  `DELETE /api/v1/o/acme/roles/${id}/permissions/${code}/`;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const role = (id: string, name: string, extra: Partial<Role> = {}): Role => ({
  id,
  code: `c-${id}`, // distinto del id: la ruta pide el id
  name,
  description: "",
  is_system: false,
  permissions: [],
  members: 0,
  editable: true,
  ...extra,
});
const permission = (code: string, extra: Partial<Permission> = {}): Permission => ({
  code,
  module: code.split(".")[0]!,
  is_sensitive: false,
  supports_scope: false,
  ...extra,
});
const everything = [
  permission("audit.view", { is_sensitive: true }),
  permission("roles.view"),
  permission("sales.orders.view", { supports_scope: true }), // sin texto propio y con alcance
  permission("users.view"),
];
const caja = role("r2", "Caja", {
  members: 3,
  permissions: [
    { code: "roles.view", scope: null },
    { code: "sales.orders.view", scope: "TEAM" },
  ],
});
const roles = [
  role("r1", "Owner", { is_system: true, editable: false, members: 1 }),
  caja,
  role("r3", "Vacío"),
];
const list = (results: Role[] = roles) => ({ status: 200, body: { results, next: null } });
const catalog = { status: 200, body: { results: everything } };
const done = { status: 204 };
const ui = (context = tenant("roles.view", "roles.manage")) => (
  <TenantProvider value={context}>
    <RolesList />
  </TenantProvider>
);
const card = (name: string) =>
  screen.getByText(name, { selector: ".font-medium" }).closest("li") as HTMLElement;
const grants = (name: string) =>
  within(within(card(name)).getByRole("list", { name: `Permisos de ${name}` }))
    .getAllByRole("listitem")
    .map((item) => item.textContent);
const calls = (api: ReturnType<typeof mockApi>, method: string, path = "") =>
  api.mock.calls.filter(
    ([url, init]) => (init?.method ?? "GET") === method && String(url).includes(path),
  );
const writes = (api: ReturnType<typeof mockApi>) => [...calls(api, "PUT"), ...calls(api, "DELETE")];
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
async function panel(name = "Caja") {
  await screen.findByRole("list", { name: "Roles" });
  fireEvent.click(screen.getByRole("button", { name: `Cambiar los permisos de ${name}` }));
  const group = screen.getByRole("group", { name: `Permisos de ${name}` });
  await within(group).findByRole("button", { name: /Ver miembros/ });
  return group;
}
const action = (group: HTMLElement, name: string) => within(group).getByRole("button", { name });

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("RolePermissionsAction", () => {
  it("se ofrece con el permiso y solo en los roles editables; abrir pide el catálogo y no envía nada", async () => {
    const api = mockApi({ [LIST]: list(), [CATALOG]: catalog });
    const view = renderApp(ui(tenant("roles.view")));
    await screen.findByRole("list", { name: "Roles" });
    expect(screen.queryByRole("button", { name: /Cambiar los permisos/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    await screen.findByRole("list", { name: "Roles" });
    const offered = screen.getAllByRole("button", { name: /Cambiar los permisos/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Cambiar los permisos de Caja", // el Owner no: la API lo marca como no editable
      "Cambiar los permisos de Vacío",
    ]);
    expect(calls(api, "GET", "/permissions/")).toHaveLength(0); // nada hasta abrir
    const group = await panel();
    expect(calls(api, "GET", "/permissions/")).toHaveLength(1);
    expect(within(group).getByRole("button", { name: "Cerrar" })).toHaveFocus();
    expect(group).toHaveTextContent("Lo que cambies llega enseguida a sus 3 miembros.");
    const rows = within(group)
      .getAllByRole("listitem")
      .map((item) => item.textContent);
    expect(rows).toEqual([
      "Ver la auditoríaSensibleConceder",
      "Ver rolesRetirar", // lo que el rol ya tiene se puede retirar
      "sales.orders.viewSu equipo", // con alcance: se enseña, sin botón
      "Ver miembrosConceder",
    ]);
    expect(action(group, "Conceder Ver la auditoría a Caja")).toBeVisible();
    expect(action(group, "Retirar Ver roles a Caja")).toBeVisible();
    fireEvent.click(within(group).getByRole("button", { name: "Cerrar" }));
    expect(screen.getByRole("button", { name: "Cambiar los permisos de Caja" })).toHaveFocus();
    expect(writes(api)).toHaveLength(0);
    const empty = await panel("Vacío");
    expect(empty).toHaveTextContent("Ningún miembro tiene este rol todavía.");
    expect(empty).toHaveTextContent("sales.orders.viewSin conceder (con alcance)");
  });

  it("concede y retira con un solo envío, cambia la tarjeta sin volver a pedir la lista y lo anuncia", async () => {
    const api = mockApi({
      [LIST]: list(),
      [CATALOG]: catalog,
      [GRANT("r2", "users.view")]: done,
      [REVOKE("r2", "users.view")]: done,
    });
    renderApp(ui());
    const group = await panel();
    const button = action(group, "Conceder Ver miembros a Caja");
    button.focus();
    const release = hold(api);
    fireEvent.click(button);
    fireEvent.click(button); // mientras se envía: un solo cambio
    fireEvent.click(action(group, "Retirar Ver roles a Caja")); // ni otro distinto
    fireEvent.click(within(group).getByRole("button", { name: "Cerrar" })); // ni se cierra
    await tick();
    expect(button).toHaveTextContent("Concediendo…");
    expect(button).toHaveAttribute("aria-busy", "true");
    release();
    const flipped = await within(group).findByRole("button", {
      name: "Retirar Ver miembros a Caja",
    });
    expect(flipped).toHaveFocus(); // el mismo botón, con la acción contraria
    expect(writes(api)).toHaveLength(1);
    const [url, init] = calls(api, "PUT")[0]!;
    expect(String(url)).toBe("/api/v1/o/acme/roles/r2/permissions/users.view/"); // el id del rol
    expect(JSON.parse(String(init?.body))).toEqual({});
    expect(grants("Caja")).toEqual(["Ver roles", "sales.orders.viewSu equipo", "Ver miembros"]);
    expect(calls(api, "GET", "/roles/")).toHaveLength(1); // la lista no se vuelve a pedir
    expect(
      within(card("Caja")).getByText("Permiso Ver miembros concedido a Caja."),
    ).toHaveAttribute("role", "status");
    fireEvent.click(flipped, { detail: 2 }); // el segundo clic de un doble clic no lo deshace
    await tick();
    expect(writes(api)).toHaveLength(1);
    fireEvent.click(flipped);
    await within(group).findByRole("button", { name: "Conceder Ver miembros a Caja" });
    expect(calls(api, "DELETE")).toHaveLength(1);
    expect(grants("Caja")).toEqual(["Ver roles", "sales.orders.viewSu equipo"]);
    expect(within(card("Caja")).getByText("Permiso Ver miembros retirado a Caja.")).toBeTruthy();
    expect(grants("Vacío")).toEqual(["Sin permisos"]); // solo cambia ese rol
  });

  it.each([
    [403, { code: "PERMISSION_DENIED" }, "No puedes hacer este cambio"],
    [409, { code: "LAST_OWNER" }, "Debe quedar al menos un Owner activo"],
    [400, { code: "VALIDATION_ERROR", fields: { scope: [] } }, "Algo salió mal de nuestro lado"],
    [500, { code: "INTERNAL_ERROR", message: "texto de la API" }, "Algo salió mal de nuestro lado"],
  ])(
    "un %s se explica en el panel, por su código, y el mismo botón reintenta",
    async (status, body, text) => {
      const api = mockApi({
        [LIST]: list(),
        [CATALOG]: catalog,
        [GRANT("r2", "audit.view")]: { status, body },
      });
      renderApp(ui());
      const group = await panel();
      const button = action(group, "Conceder Ver la auditoría a Caja");
      fireEvent.click(button);
      expect(await within(group).findByRole("alert")).toHaveTextContent(text);
      expect(group).not.toHaveTextContent("texto de la API");
      expect(button).toHaveTextContent("Conceder"); // nada cambió
      expect(grants("Caja")).toEqual(["Ver roles", "sales.orders.viewSu equipo"]);
      fireEvent.click(button); // la marca de envío se soltó
      await waitFor(() => expect(writes(api)).toHaveLength(2));
      fireEvent.click(within(group).getByRole("button", { name: "Cerrar" }));
      const again = await panel();
      expect(within(again).queryByRole("alert")).not.toBeInTheDocument(); // al reabrir, sin el error
    },
  );

  it("sin red el intento falla y se dice: no queda en cola", async () => {
    const api = mockApi({ [LIST]: list(), [CATALOG]: catalog, [GRANT("r2", "users.view")]: done });
    renderApp(ui());
    const group = await panel();
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(action(group, "Conceder Ver miembros a Caja"));
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(writes(api)).toHaveLength(1);
  });

  it("un 404 es pantalla desfasada: cierra el panel, vuelve a pedir la lista y lo explica fuera", async () => {
    let rows = roles;
    const api = mockApi({
      [LIST]: () => list(rows),
      [CATALOG]: catalog,
      [REVOKE("r2", "roles.view")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await panel();
    rows = [roles[0]!, { ...caja, permissions: [] }, roles[2]!]; // otra persona ya lo retiró
    const button = action(group, "Retirar Ver roles a Caja");
    button.focus();
    fireEvent.click(button);
    await waitFor(() => expect(calls(api, "GET", "/roles/")).toHaveLength(2));
    await waitFor(() => expect(grants("Caja")).toEqual(["Sin permisos"]));
    expect(screen.queryByRole("group", { name: "Permisos de Caja" })).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "El rol Caja o sus permisos ya habían cambiado. Revisa la lista.",
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    await panel("Vacío"); // al abrir otro panel, el aviso ya no aplica
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("si el catálogo no llega ofrece reintentar, y al llegar el foco pasa a «Cerrar»", async () => {
    let answer: { status: number; body: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    const api = mockApi({ [LIST]: list(), [CATALOG]: () => answer });
    renderApp(ui());
    await screen.findByRole("list", { name: "Roles" });
    fireEvent.click(screen.getByRole("button", { name: "Cambiar los permisos de Caja" }));
    const group = screen.getByRole("group", { name: "Permisos de Caja" });
    expect(await within(group).findByRole("alert")).toHaveTextContent("Algo salió mal");
    const retry = within(group).getByRole("button", { name: "Reintentar" });
    retry.focus();
    answer = catalog;
    fireEvent.click(retry);
    await within(group).findByRole("button", { name: /Ver miembros/ });
    expect(within(group).getByRole("button", { name: "Cerrar" })).toHaveFocus();
    expect(within(group).queryByRole("alert")).not.toBeInTheDocument();
    expect(writes(api)).toHaveLength(0);
  });

  it("sin sesión va al login una vez y el panel sigue ocupado, sin error", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/roles", search: "", assign });
    const api = mockApi({
      [LIST]: list(),
      [CATALOG]: catalog,
      [GRANT("r2", "users.view")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const group = await panel();
    const button = action(group, "Conceder Ver miembros a Caja");
    fireEvent.click(button);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Froles"));
    await tick();
    fireEvent.click(button); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(group).getByRole("button", { name: "Cerrar" }));
    await tick();
    expect(writes(api)).toHaveLength(1);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(button).toHaveTextContent("Concediendo…");
  });
});
