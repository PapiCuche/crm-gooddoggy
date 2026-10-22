import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Brand, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { BrandsList } from "./brands-list";

const LIST = "GET /api/v1/o/acme/brands/";
const CREATE = "POST /api/v1/o/acme/brands/";
const RENAME = (id: string) => `PATCH /api/v1/o/acme/brands/${id}/`;
const base: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "products.view", scopes: [] }],
};
const manager: SelfContext = {
  ...base,
  permissions: [...base.permissions, { code: "brands.manage", scopes: [] }],
};
const apple: Brand = { id: "b1", name: "Apple", slug: "apple" };
const page = (results: Brand[]) => ({ status: 200, body: { results, next: null } });
type Reply = { status: number; body?: unknown };

const screenOf = (tenant: SelfContext) =>
  renderApp(
    <TenantProvider value={tenant}>
      <BrandsList />
    </TenantProvider>,
  );
const posts = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => init?.method === method);

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => vi.unstubAllGlobals());

describe("BrandsList", () => {
  it("enseña las marcas de la API; sin `brands.manage` no ofrece escribir", async () => {
    mockApi({ [LIST]: page([apple, { id: "b2", name: "Samsung Perú", slug: "samsung-peru" }]) });
    screenOf(base);
    expect(screen.getByRole("status")).toHaveTextContent("Cargando marcas");
    const list = await screen.findByRole("list", { name: "Marcas" });
    expect(within(list).getByText("Samsung Perú")).toHaveClass("wrap-anywhere");
    expect(within(list).getByText("samsung-peru")).toHaveClass("font-mono");
    expect(screen.getByText("Good Doggy / Catálogo")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("2 marcas en la lista");
    expect(screen.queryByRole("button", { name: "Crear marca" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Renombrar/ })).toBeNull();
  });

  it("sin marcas lo dice; sin permiso, la API decide y la pantalla lo explica", async () => {
    mockApi({ [LIST]: page([]) });
    screenOf(base);
    expect(await screen.findByText("Esta organización todavía no tiene marcas.")).toBeVisible();
    vi.unstubAllGlobals();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf(base);
    expect(
      await screen.findByText("No tienes permiso para ver el catálogo de esta organización."),
    ).toBeVisible();
  });

  it("crea una marca: el slug vacío no se envía y uno ocupado se dice en su campo", async () => {
    let create: Reply = { status: 409, body: { code: "BRAND_SLUG_TAKEN" } };
    let listed: Brand[] = [];
    const api = mockApi({ [LIST]: () => page(listed), [CREATE]: () => create });
    screenOf(manager);
    fireEvent.click(await screen.findByRole("button", { name: "Crear marca" }));
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Apple" } });
    fireEvent.change(screen.getByLabelText("Identificador (opcional)"), {
      target: { value: "apple" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Crear marca" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Identificador (opcional)")).toHaveAccessibleDescription(
        /Ya hay una marca con ese identificador/,
      ),
    );
    create = { status: 201, body: apple };
    listed = [apple];
    fireEvent.change(screen.getByLabelText("Identificador (opcional)"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Crear marca" }));
    expect(await screen.findByText("Marca «Apple» creada.")).toBeVisible();
    const sent = posts(api, "POST").map(([, init]) => JSON.parse(String(init?.body)));
    expect(sent).toEqual([{ name: "Apple", slug: "apple" }, { name: "Apple" }]);
    expect(await screen.findByText("apple")).toBeVisible(); // la lista se vuelve a pedir
  });

  it("renombra una marca y la fila cambia sin tocar el slug", async () => {
    const api = mockApi({
      [LIST]: page([apple]),
      [RENAME("b1")]: { status: 200, body: { ...apple, name: "Apple Inc." } },
    });
    screenOf(manager);
    fireEvent.click(await screen.findByRole("button", { name: "Renombrar Apple" }));
    expect(screen.getByLabelText("Nombre")).toHaveValue("Apple");
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Apple Inc." } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByText("Apple Inc.")).toBeVisible();
    expect(screen.getByText("apple")).toBeVisible();
    expect(JSON.parse(String(posts(api, "PATCH")[0]?.[1]?.body))).toEqual({ name: "Apple Inc." });
  });

  it("si la marca ya no existe, la lista avisa y se vuelve a pedir", async () => {
    let listed = [apple];
    mockApi({
      [LIST]: () => page(listed),
      [RENAME("b1")]: () => {
        listed = [];
        return { status: 404, body: { code: "NOT_FOUND" } };
      },
    });
    screenOf(manager);
    fireEvent.click(await screen.findByRole("button", { name: "Renombrar Apple" }));
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Otra" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "«Apple» ya no está en el catálogo: la lista se actualizó.",
    );
    expect(await screen.findByText("Esta organización todavía no tiene marcas.")).toBeVisible();
  });
});
