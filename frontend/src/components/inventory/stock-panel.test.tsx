import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { StockPanel } from "./stock-panel";

const API = "/api/v1/o/acme";
const WAREHOUSES = `GET ${API}/warehouses/?limit=200`;
const PRODUCTS = `GET ${API}/products/?limit=200`;
const VARIANTS = (id: string) => `GET ${API}/products/${id}/variants/?limit=200`;
const LEVELS = (id: string) => `GET ${API}/inventory/levels/?limit=200&variant_id=${id}`;
const STOCK = (id: string, more = "&has_stock=true") =>
  `GET ${API}/inventory/levels/?warehouse_id=${id}${more}`;
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "inventory.view", scopes: [] }],
};
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const warehouse = (id: string, name: string, code: string, is_active = true) => ({
  ...{ id, name, code, is_active, branch_id: "b1", type: "WAREHOUSE", is_sellable: true },
});
const product = (id: string, name: string, tracks_inventory = true) => ({
  ...{ id, name, slug: id, tracks_inventory, product_type: "PHYSICAL", status: "ACTIVE" },
  ...{ brand: null, category: null, short_description: "", description: "", ai_visible: false },
});
const variant = (id: string, name: string, sku: string) => ({
  ...{ id, name, sku, label: "", barcode: "", condition: "NEW", status: "ACTIVE" },
  ...{ is_default: true, ai_visible: false },
});
const label = (id: string, name: string, sku: string, product_id: string, is_deleted = false) => ({
  ...{ id, name, sku, product_id, is_deleted },
});
const COLLAR = label("v1", "Collar", "COL-1", "p2");
const level = (warehouse_id: string, on_hand: number, reserved: number, variant = COLLAR) => ({
  ...{ id: `l-${warehouse_id}-${variant.id}`, variant_id: variant.id, variant, warehouse_id },
  ...{ on_hand, reserved, available: on_hand - reserved },
});
const ROUTES = {
  [WAREHOUSES]: page([warehouse("w1", "Central", "CEN"), warehouse("w2", "Surco", "SUR", false)]),
  [PRODUCTS]: page([product("p1", "Baño"), product("p2", "Collar"), product("p3", "Corte", false)]),
  [VARIANTS("p2")]: page([variant("v1", "Collar", "COL-1")]),
  [VARIANTS("p1")]: page([variant("v2", "Baño M", "B-M"), variant("v3", "Baño L", "B-L")]),
  [LEVELS("v1")]: page([level("w2", 3, 0), level("w1", 10, 4)]),
  [LEVELS("v3")]: page([]),
  [STOCK("w1")]: page([level("w1", 10, 4)]),
  [STOCK("w2")]: page([]),
};

const screenOf = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <StockPanel />
    </TenantProvider>,
  );
const field = (label: string) => screen.getByLabelText(label);
const pick = (label: string, value: string) =>
  fireEvent.change(field(label), { target: { value } });
const options = (label: string) =>
  [...(field(label) as HTMLSelectElement).options].map((option) => option.text);
const figures = (box: HTMLElement) =>
  within(box)
    .getAllByRole("definition")
    .map((cell) => cell.textContent);

afterEach(() => vi.unstubAllGlobals());

describe("StockPanel", () => {
  it("elige almacén, busca el producto y ve su saldo aquí y en cada almacén", async () => {
    mockApi(ROUTES);
    screenOf();
    await waitFor(() => expect(options("Almacén")).toHaveLength(3));
    expect(options("Almacén")).toEqual([
      "Elige un almacén",
      "Central (CEN)",
      "Surco (SUR), inactivo",
    ]);
    await waitFor(() =>
      expect(options("Producto")).toEqual(["Elige un producto", "Baño", "Collar"]),
    );
    expect(screen.getByText(/Elige un almacén, un producto/)).toBeVisible();
    fireEvent.change(field("Buscar producto"), { target: { value: "  bano " } });
    expect(options("Producto")).toEqual(["Elige un producto", "Baño"]); // sin tildes ni mayúsculas
    fireEvent.change(field("Buscar producto"), { target: { value: "xyz" } });
    expect(field("Buscar producto")).toHaveAccessibleDescription(/Ningún producto/);
    fireEvent.change(field("Buscar producto"), { target: { value: "col" } });
    pick("Producto", "p2");
    pick("Almacén", "w1");
    // Una sola variante se elige sola.
    const here = await screen.findByRole("heading", { name: "Collar en Central" });
    expect(field("Variante")).toHaveValue("v1");
    expect(options("Variante")).toEqual(["Elige una variante", "Collar · COL-1"]);
    expect(figures(here.parentElement!)).toEqual(["10", "4", "6"]);
    const all = within(screen.getByRole("list", { name: "En todos los almacenes" }));
    const [surco, central] = all.getAllByRole("listitem");
    expect(surco).toHaveTextContent("Surco (SUR), inactivo");
    expect(figures(surco!)).toEqual(["3", "0", "3"]);
    expect(figures(central!)).toEqual(["10", "4", "6"]);
    fireEvent.change(field("Buscar producto"), { target: { value: "baño" } });
    expect(options("Producto")).toEqual(["Elige un producto", "Baño", "Collar"]); // lo elegido sigue
  });

  it("con varias variantes hay que elegir; sin movimientos, el saldo es cero", async () => {
    mockApi({ ...ROUTES, [LEVELS("v1")]: page([level("w1", 10, 4)]) });
    screenOf();
    await waitFor(() => expect(options("Producto")).toHaveLength(3));
    pick("Almacén", "w2");
    pick("Producto", "p1");
    await waitFor(() => expect(options("Variante")).toHaveLength(3));
    expect(field("Variante")).toHaveValue("");
    expect(screen.queryByRole("heading", { name: /^Baño/ })).toBeNull();
    pick("Variante", "v3");
    const here = await screen.findByRole("heading", { name: "Baño L en Surco" });
    expect(figures(here.parentElement!)).toEqual(["0", "0", "0"]);
    expect(screen.getByText(/todavía no tiene movimientos/)).toBeVisible();
    pick("Producto", "p2"); // cambiar de producto deja la variante de antes
    const surco = await screen.findByRole("heading", { name: "Collar en Surco" });
    expect(figures(surco.parentElement!)).toEqual(["0", "0", "0"]); // movió en Central, no aquí
  });

  it("sin almacenes o sin productos dice qué falta y dónde se crea", async () => {
    mockApi({ ...ROUTES, [WAREHOUSES]: page([]) });
    const view = screenOf();
    const link = await screen.findByRole("link", { name: "Crear un almacén" });
    expect(link).toHaveAttribute("href", "/o/acme/inventario/almacenes");
    view.unmount();
    mockApi({ ...ROUTES, [PRODUCTS]: page([product("p3", "Corte", false)]) });
    screenOf();
    const create = await screen.findByRole("link", { name: "Crear un producto" });
    expect(create).toHaveAttribute("href", "/o/acme/catalogo/productos");
  });

  it("con solo el almacén, todo su stock con SKU y nombre; una fila abre su celda", async () => {
    const BAG = label("v4", "Bolsa", "BOL-9", "p9", true);
    const api = mockApi({
      ...ROUTES,
      [STOCK("w1")]: page([level("w1", 10, 4), level("w1", 2, 0, BAG)], "c2"),
      [STOCK("w1", "&has_stock=true&cursor=c2")]: page([
        level("w1", 1, 1, label("v2", "Baño M", "B-M", "p1")),
      ]),
      [STOCK("w1", "")]: page([level("w1", 0, 0, label("v3", "Baño L", "B-L", "p1"))]),
    });
    screenOf();
    await waitFor(() => expect(options("Almacén")).toHaveLength(3));
    expect(screen.getByText("Elige un almacén para ver todo lo que tiene.")).toBeVisible();
    pick("Almacén", "w1");
    const list = await screen.findByRole("list", { name: "Existencias" });
    expect(screen.getByRole("heading", { name: "Todo lo que hay en Central" })).toBeVisible();
    const [collar, bag] = within(list).getAllByRole("listitem");
    expect(within(collar!).getByText("Collar")).toBeVisible();
    expect(within(collar!).getByText("COL-1")).toBeVisible();
    expect(figures(collar!)).toEqual(["10", "4", "6"]);
    expect(within(bag!).getByText("BOL-9 · borrada del catálogo")).toBeVisible();
    expect(within(bag!).queryByRole("button")).toBeNull(); // borrada: no se elige
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await within(list).findByText("Baño M")).toBeVisible();
    const stockCalls = () =>
      api.mock.calls.map(([url]) => String(url)).filter((url) => url.includes("warehouse_id"));
    expect(stockCalls()).toEqual([
      `${API}/inventory/levels/?warehouse_id=w1&has_stock=true`,
      `${API}/inventory/levels/?warehouse_id=w1&has_stock=true&cursor=c2`,
    ]);
    fireEvent.click(screen.getByLabelText(/también las variantes que se quedaron en cero/));
    expect(await screen.findByText("Baño L")).toBeVisible(); // sin `has_stock`
    fireEvent.click(screen.getByRole("button", { name: "Ver detalle: Baño L" }));
    const here = await screen.findByRole("heading", { name: "Baño L en Central" });
    expect(field("Producto")).toHaveValue("p1");
    expect(field("Variante")).toHaveValue("v3");
    expect(here.closest("[tabindex='-1']")).toHaveFocus();
  });

  it("con `inventory.adjust`, la fila abre también su formulario; sin filas lo dice", async () => {
    mockApi(ROUTES);
    const adjust = { code: "inventory.adjust", scopes: [] };
    renderApp(
      <TenantProvider value={{ ...tenant, permissions: [...tenant.permissions, adjust] }}>
        <StockPanel />
      </TenantProvider>,
    );
    await waitFor(() => expect(options("Almacén")).toHaveLength(3));
    pick("Almacén", "w2");
    expect(await screen.findByText("Este almacén no tiene existencias.")).toBeVisible();
    pick("Almacén", "w1");
    fireEvent.click(await screen.findByRole("button", { name: "Ver y registrar: Collar" }));
    expect(await screen.findByRole("heading", { name: "Collar en Central" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Registrar movimiento" })).toBeVisible();
  });

  it("explica las negativas y los fallos por `code`, y reintenta", async () => {
    mockApi({ ...ROUTES, [WAREHOUSES]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    const view = screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent(/ver el inventario/);
    expect(screen.queryByLabelText("Producto")).toBeNull();
    view.unmount();
    mockApi({ ...ROUTES, [PRODUCTS]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf();
    await waitFor(() =>
      expect(field("Producto")).toHaveAccessibleDescription(
        /No tienes permiso para ver el catálogo/,
      ),
    );
    expect(screen.queryByRole("button", { name: "Reintentar" })).toBeNull(); // un 403 no se reintenta
  });

  it("si el saldo no llega lo dice, y «Reintentar» lo vuelve a pedir", async () => {
    let down = true;
    mockApi({
      ...ROUTES,
      [LEVELS("v1")]: () => (down ? { status: 500, body: {} } : ROUTES[LEVELS("v1")]!),
    });
    screenOf();
    await waitFor(() => expect(options("Producto")).toHaveLength(3));
    pick("Almacén", "w1");
    pick("Producto", "p2");
    expect(await screen.findByRole("alert", {}, { timeout: 3000 })).toHaveTextContent(
      "Algo salió mal",
    );
    down = false;
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByRole("heading", { name: "Collar en Central" })).toBeVisible();
  });
});
