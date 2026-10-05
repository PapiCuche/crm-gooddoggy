import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Branch, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { BranchesList } from "./branches-list";

const LIST = "GET /api/v1/o/acme/branches/";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "organization.view", scopes: [] }],
};
const branch = (code: string, extra: Partial<Branch> = {}): Branch => ({
  id: code,
  code,
  name: code,
  address: "",
  district: "",
  city: "",
  phone: "",
  timezone: "America/Lima",
  is_active: true,
  ...extra,
});
const lima = branch("LIM-01", {
  name: "Centro de Lima",
  address: "Av. Wilson 1234",
  district: "Cercado",
  city: "Lima",
  phone: "+51 1 555 0100",
});
const screenOf = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <BranchesList />
    </TenantProvider>,
  );
const card = (name: string) =>
  screen.getByText(name, { selector: "span.font-medium" }).closest("li") as HTMLElement;
const lines = (name: string) => [...card(name).querySelectorAll("p")].map((p) => p.textContent);

afterEach(() => vi.unstubAllGlobals());

describe("BranchesList", () => {
  it("muestra lo que devuelve la API: nombre, código, dirección, teléfono, zona y estado", async () => {
    const closed = branch("AQP", { name: "Arequipa", city: " Arequipa ", is_active: false });
    const bare = branch("CUZ", { name: "Cusco", district: "  ", phone: " ", timezone: "UTC" });
    mockApi({ [LIST]: { status: 200, body: { results: [lima, closed, bare], next: null } } });
    screenOf();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando sucursales");
    expect(await screen.findByRole("list", { name: "Sucursales" })).toBeVisible();
    expect(lines("Centro de Lima")).toEqual([
      "Centro de LimaLIM-01",
      "Av. Wilson 1234, Cercado, Lima", // lo que hay, en una línea
      "Teléfono: +51 1 555 0100",
      "Zona horaria: America/Lima",
      "Activa",
    ]);
    expect(lines("Arequipa")).toEqual([
      "ArequipaAQP",
      " Arequipa ", // solo la ciudad: sin comas sueltas
      "Zona horaria: America/Lima",
      "Inactiva", // también las inactivas
    ]);
    expect(lines("Cusco")).toEqual(["CuscoCUZ", "Sin dirección", "Zona horaria: UTC", "Activa"]);
    expect(screen.getByRole("status")).toHaveTextContent("3 sucursales en la lista");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Sucursales");
    expect(screen.getByText("Las sucursales y tiendas de Acme SAC.")).toBeVisible();
    expect(screen.queryByText("Esta organización todavía no tiene sucursales.")).toBeNull();
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); // solo lectura, una página
  });

  it("un texto de la API con llaves o etiquetas se enseña tal cual", async () => {
    const odd = branch("X", {
      name: "<b>{count}</b>",
      phone: "{phone} #",
      timezone: "{x, plural}",
    });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    mockApi({ [LIST]: { status: 200, body: { results: [odd], next: null } } });
    screenOf();
    await screen.findByRole("list", { name: "Sucursales" });
    expect(lines("<b>{count}</b>")).toEqual([
      "<b>{count}</b>X",
      "Sin dirección",
      "Teléfono: {phone} #",
      "Zona horaria: {x, plural}",
      "Activa",
    ]);
    expect(screen.getByRole("status")).toHaveTextContent("1 sucursal en la lista");
    expect(errors).not.toHaveBeenCalled(); // ningún texto sin resolver
    errors.mockRestore();
  });

  it("una organización sin sucursales lo dice, sin error", async () => {
    mockApi({ [LIST]: { status: 200, body: { results: [], next: null } } });
    screenOf();
    expect(await screen.findByText("Esta organización todavía no tiene sucursales.")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Sin sucursales en la lista");
    expect(screen.queryByRole("list")).not.toBeInTheDocument(); // ni una lista vacía
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("pide sus propios datos, con la organización del contexto", async () => {
    const api = mockApi({ [LIST]: { status: 200, body: { results: [lima], next: null } } });
    const view = screenOf();
    await screen.findByRole("list", { name: "Sucursales" });
    const cached = view.client
      .getQueryCache()
      .getAll()
      .map((query) => query.queryKey);
    expect(cached).toEqual([["/api/v1/o/acme/branches/", "pages"]]);
    expect(api).toHaveBeenCalledTimes(1);
  });

  it("carga la página siguiente con el cursor y deja el foco en lo que llegó", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [lima], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [branch("AQP")], next: null } },
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("2 sucursales"));
    expect(api).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(card("AQP")).toHaveFocus());
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
      "No tienes permiso para ver las sucursales de esta organización.",
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument(); // decide la API
    denied.unmount();
    reply = { status: 401, body: { code: "NOT_AUTHENTICATED" } };
    const ended = screenOf();
    await waitFor(() => expect(ended.client.isFetching()).toBe(0));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument(); // el login lo decide el proveedor
    expect(screen.getByRole("status")).toHaveTextContent("Cargando sucursales");
    ended.unmount();
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    reply = { status: 200, body: { results: [lima], next: null } };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByRole("list", { name: "Sucursales" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
  });
});
