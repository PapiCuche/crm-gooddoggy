import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Role, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { RolesList } from "./roles-list";

const LIST = "GET /api/v1/o/acme/roles/";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "roles.view", scopes: [] }],
};
const role = (code: string, extra: Partial<Role> = {}): Role => ({
  id: code,
  code,
  name: code,
  description: "",
  is_system: false,
  permissions: [],
  members: 0,
  editable: true,
  ...extra,
});
const owner = role("owner", {
  name: "Owner",
  is_system: true,
  members: 1,
  permissions: [
    { code: "audit.view", scope: null },
    { code: "users.manage", scope: null },
    { code: "sales.orders.view", scope: "TEAM" }, // estos textos aún no lo conocen
    { code: "Código raro", scope: "ORGANIZATION" },
  ],
});
const screenOf = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <RolesList />
    </TenantProvider>,
  );
const card = (name: string) =>
  screen.getByText(name, { selector: ".font-medium" }).closest("li") as HTMLElement;
const grants = (name: string) =>
  within(within(card(name)).getByRole("list", { name: `Permisos de ${name}` }))
    .getAllByRole("listitem")
    .map((item) => item.textContent);

afterEach(() => vi.unstubAllGlobals());

describe("RolesList", () => {
  it("muestra lo que devuelve la API: nombre, origen, miembros y permisos con su alcance", async () => {
    const custom = role("caja", { name: "Caja", description: "Cobra en tienda", members: 3 });
    mockApi({
      [LIST]: { status: 200, body: { results: [owner, custom, role("vacio")], next: null } },
    });
    screenOf();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando roles");
    expect(await screen.findByRole("list", { name: "Roles" })).toBeVisible();
    expect(card("Owner")).toHaveTextContent("De plantilla");
    expect(within(card("Owner")).getByText("1 miembro")).toBeVisible(); // exacto: singular
    expect(grants("Owner")).toEqual([
      "Ver la auditoría", // el nombre del permiso, no su código
      "Administrar miembros",
      "sales.orders.viewSu equipo", // sin texto propio: su código, con su alcance
      "Código raroToda la organización",
    ]);
    expect(card("Caja")).toHaveTextContent("Propio");
    expect(card("Caja")).toHaveTextContent("Cobra en tienda");
    expect(card("Caja")).toHaveTextContent("3 miembros");
    expect(grants("Caja")).toEqual(["Sin permisos"]);
    expect(card("vacio")).toHaveTextContent("Sin miembros");
    expect(card("vacio").querySelectorAll("p")).toHaveLength(2); // sin descripción: sin párrafo vacío
    expect(screen.getByRole("status")).toHaveTextContent("3 roles en la lista");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Roles");
    expect(screen.getByText(/Los roles de Acme SAC/)).toBeVisible();
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); // solo lectura, una página
  });

  it("cada permiso del catálogo y cada alcance tiene su texto, y lo demás se enseña tal cual", async () => {
    const codes = ["audit.view", "organization.manage", "organization.view", "roles.manage"];
    const scopes = ["OWN", "TEAM", "BRANCH", "ORGANIZATION"] as const;
    const odd = ["users", "users.constructor.name", "users.view.length", "constructor.prototype"];
    const all = role("todo", {
      description: "  ", // solo espacios: como si no tuviera
      permissions: [
        ...codes.map((code, index) => ({ code, scope: scopes[index]! })),
        ...[
          "roles.view",
          "users.invite",
          "users.manage",
          "users.view",
          "branches.manage",
          "teams.view",
        ].map((code) => ({
          code,
          scope: null,
        })),
        ...odd.map((code) => ({ code, scope: null })), // rutas de mensajes que no son un permiso
        { code: "x.y", scope: "REGION" as never }, // un alcance que estos textos no conocen
      ],
    });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    mockApi({ [LIST]: { status: 200, body: { results: [all], next: null } } });
    screenOf();
    await screen.findByRole("list", { name: "Roles" });
    const shown = within(within(card("todo")).getByRole("list", { name: "Permisos de todo" }))
      .getAllByRole("listitem")
      .map((item) => [...item.children].map((part) => part.textContent));
    expect(shown).toEqual([
      ["Ver la auditoría", "Lo propio"],
      ["Administrar la organización", "Su equipo"],
      ["Ver la organización", "Su sucursal"],
      ["Administrar roles", "Toda la organización"],
      ["Ver roles"],
      ["Invitar miembros"],
      ["Administrar miembros"],
      ["Ver miembros"],
      ["Administrar sucursales"],
      ["Ver equipos"],
      ...odd.map((code) => [code]),
      ["x.y", "REGION"],
    ]);
    expect(card("todo").querySelectorAll("p")).toHaveLength(2);
    expect(screen.getByRole("status")).toHaveTextContent("1 rol en la lista");
    expect(errors).not.toHaveBeenCalled(); // ningún texto sin resolver
    errors.mockRestore();
  });

  it("al llegar desde otra lista pide y muestra sus propios datos", async () => {
    const api = mockApi({
      "GET /api/v1/o/acme/members/": { status: 200, body: { results: [], next: null } },
      [LIST]: { status: 200, body: { results: [owner], next: null } },
    });
    const view = screenOf();
    await screen.findByRole("list", { name: "Roles" });
    const cached = view.client
      .getQueryCache()
      .getAll()
      .map((query) => query.queryKey);
    expect(cached).toEqual([["/api/v1/o/acme/roles/", "pages"]]); // su clave, con su organización
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("carga la página siguiente con el cursor y deja el foco en lo que llegó", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [owner], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [role("caja")], next: null } },
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("2 roles"));
    expect(api).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(card("caja")).toHaveFocus());
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
  });

  it("sin el permiso lo explica; sin sesión no enseña un error; un fallo se puede reintentar", async () => {
    let reply: { status: number; body: unknown } = {
      status: 403,
      body: { code: "PERMISSION_DENIED" },
    };
    mockApi({ [LIST]: () => reply });
    const denied = screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver los roles de esta organización.",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); // decide la API
    denied.unmount();
    reply = { status: 401, body: { code: "NOT_AUTHENTICATED" } };
    const ended = screenOf();
    await waitFor(() => expect(ended.client.isFetching()).toBe(0));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // el login lo decide el proveedor
    expect(screen.getByRole("status")).toHaveTextContent("Cargando roles");
    ended.unmount();
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    reply = { status: 200, body: { results: [owner], next: null } };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByRole("list", { name: "Roles" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
  });
});
