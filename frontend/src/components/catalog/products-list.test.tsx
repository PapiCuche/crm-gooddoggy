import { onlineManager } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Product, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ProductsList } from "./products-list";

const LIST = "GET /api/v1/o/acme/products/";
const CREATE = "POST /api/v1/o/acme/products/";
const BRANDS = "GET /api/v1/o/acme/brands/?limit=200";
const CATEGORIES = "GET /api/v1/o/acme/categories/?limit=200";
const base: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "products.view", scopes: [] }],
};
const manager: SelfContext = {
  ...base,
  permissions: [...base.permissions, { code: "products.manage", scopes: [] }],
};
const product = (id: string, name: string, extra: Partial<Product> = {}): Product => ({
  id,
  name,
  slug: name.toLowerCase(),
  product_type: "PHYSICAL",
  status: "DRAFT",
  brand: null,
  category: null,
  short_description: "",
  description: "",
  tracks_inventory: true,
  ai_visible: false,
  ...extra,
});
const collar = product("p1", "Collar", {
  status: "ACTIVE",
  brand: { id: "b1", name: "Kong" },
  category: { id: "c2", name: "Collares" },
});
const bath = product("p2", "Baño", {
  product_type: "SERVICE",
  status: "DISCONTINUED",
  category: { id: "c3", name: "Spa" },
});
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const cat = (id: string, name: string, path: string, parent_id: string | null) => ({
  id,
  name,
  path,
  parent_id,
  slug: name.toLowerCase(),
  depth: path.split("/").length - 2,
});
const OPTIONS = {
  [BRANDS]: page([{ id: "b1", name: "Kong", slug: "kong" }]),
  [CATEGORIES]: page([
    cat("c2", "Collares", "/perros/collares/", "c1"),
    cat("c1", "Perros", "/perros/", null),
  ]),
};

const screenOf = (tenant: SelfContext) =>
  renderApp(
    <TenantProvider value={tenant}>
      <ProductsList />
    </TenantProvider>,
  );
const bodies = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body)));
const form = () => screen.getByRole("form", { name: "Nuevo producto" });
const field = (label: string) => within(form()).getByLabelText(label);
const alert = () => within(form()).getByRole("alert");
// Como una pulsación real: el botón recibe el foco antes del clic.
function press(name: string, within_: HTMLElement = document.body) {
  const button = within(within_).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
function fill(values: Record<string, string>, open = true) {
  if (open) press("Crear producto");
  for (const [label, value] of Object.entries(values)) {
    fireEvent.change(field(label), { target: { value } });
  }
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("ProductsList", () => {
  it("enseña los productos con estado, tipo, marca y categoría; sin `products.manage` no ofrece crear", async () => {
    const api = mockApi({ [LIST]: page([collar, bath]) });
    screenOf(base);
    const list = await screen.findByRole("list", { name: "Productos" });
    const [first, second] = within(list).getAllByRole("listitem");
    expect(first).toHaveTextContent("Collar");
    expect(within(first!).getByText("collar")).toHaveClass("font-mono");
    expect(within(first!).getByText("Activo")).toHaveClass("text-success");
    for (const text of ["Físico", "Marca: Kong", "Categoría: Collares"]) {
      expect(within(first!).getByText(text)).toBeVisible();
    }
    expect(within(second!).getByText("Descontinuado")).toHaveClass("text-muted");
    expect(within(second!).getByText("Servicio")).toBeVisible();
    expect(within(second!).getByText("Categoría: Spa")).toBeVisible();
    expect(second).not.toHaveTextContent("Marca");
    expect(screen.getByRole("status")).toHaveTextContent("2 productos en la lista");
    expect(screen.queryByRole("button", { name: "Crear producto" })).toBeNull();
    expect(api).toHaveBeenCalledTimes(1); // sin formulario, ni marcas ni categorías
  });

  it("sin productos lo dice; sin permiso, la API decide y la pantalla lo explica", async () => {
    const api = mockApi({ [LIST]: page([]) });
    screenOf(manager);
    expect(await screen.findByText("Esta organización todavía no tiene productos.")).toBeVisible();
    expect(api).toHaveBeenCalledTimes(1); // marcas y categorías, solo al abrir el formulario
    vi.unstubAllGlobals();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf(base);
    expect(
      await screen.findByText("No tienes permiso para ver el catálogo de esta organización."),
    ).toBeVisible();
  });

  it("crea con marca y categoría del árbol; lo vacío no se envía; un envío por pulsación", async () => {
    let rows = [bath];
    const api = mockApi({
      ...OPTIONS,
      [LIST]: () => page(rows),
      [CREATE]: () => {
        rows = [bath, collar];
        return { status: 201, body: collar };
      },
    });
    screenOf(manager);
    await screen.findByRole("list", { name: "Productos" });
    fill({ Nombre: "Collar", SKU: "col-01", "Identificador (opcional)": "collar" });
    expect(field("SKU")).toHaveAttribute("autocapitalize", "none");
    await waitFor(() => expect(field("Categoría")).toHaveLength(3));
    const options = [...(field("Categoría") as HTMLSelectElement).options];
    expect(options.map((option) => option.text.trim())).toEqual([
      "Sin categoría",
      "Perros",
      "Collares, dentro de Perros", // por rama, con la madre en el texto
    ]);
    const more = { Marca: "b1", Categoría: "c2", Tipo: "SERVICE" };
    fill({ ...more, "Descripción corta (opcional)": "  Cuero  " }, false);
    const reply = api.getMockImplementation()!;
    let release = () => {};
    api.mockImplementationOnce(async (input, init) => {
      await new Promise<void>((done) => (release = done)); // el alta tarda: se suelta a mano
      return reply(input, init);
    });
    press("Crear producto", form());
    fireEvent.submit(form()); // otra vez, enseguida
    await waitFor(() => expect(bodies(api)).toHaveLength(1));
    fill({ Marca: "" }, false); // mientras se crea, lo elegido no cambia
    expect(field("Marca")).toHaveValue("b1");
    release();
    expect(await screen.findByText("Producto «Collar» creado, en borrador.")).toBeVisible();
    expect(screen.getByText("Marca: Kong")).toBeVisible(); // la lista se volvió a pedir
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Crear producto" })).toHaveFocus(),
    );
    fill({ Nombre: "Baño", SKU: "B-1" }); // reabrir empieza de cero
    expect(screen.queryByText("Producto «Collar» creado, en borrador.")).toBeNull();
    expect(field("Tipo")).toHaveValue("PHYSICAL");
    press("Crear producto", form());
    await waitFor(() => expect(bodies(api)).toHaveLength(2));
    expect(bodies(api)).toEqual([
      {
        name: "Collar",
        sku: "col-01",
        product_type: "SERVICE",
        slug: "collar",
        brand_id: "b1",
        category_id: "c2",
        short_description: "Cuero",
      },
      { name: "Baño", sku: "B-1", product_type: "PHYSICAL" },
    ]);
  });

  it("los errores del alta se dicen por `code`, junto a su campo o en el formulario", async () => {
    let reply: { status: number; body: object } = {
      status: 409,
      body: { code: "PRODUCT_SLUG_TAKEN" },
    };
    const api = mockApi({ ...OPTIONS, [LIST]: page([]), [CREATE]: () => reply });
    screenOf(manager);
    await screen.findByText("Esta organización todavía no tiene productos.");
    fill({ Nombre: "Collar", SKU: "C1" });
    const again = async (next: typeof reply, check: () => void) => {
      reply = next;
      press("Crear producto", form());
      await waitFor(check);
    };
    await again(reply, () =>
      expect(field("Identificador (opcional)")).toHaveAccessibleDescription(
        /identificador que se forma con este nombre/,
      ),
    );
    fireEvent.change(field("Identificador (opcional)"), { target: { value: "collar" } });
    await again(reply, () =>
      expect(field("Identificador (opcional)")).toHaveAccessibleDescription(
        /Ya hay un producto con ese identificador\./,
      ),
    );
    await again({ status: 409, body: { code: "SKU_TAKEN" } }, () =>
      expect(field("SKU")).toHaveAccessibleDescription(/Ya hay una variante con ese SKU/),
    );
    expect(field("SKU")).toHaveFocus();
    const bad = (name: string) => ({
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { [name]: ["x"] } },
    });
    await again(bad("sku"), () =>
      expect(field("SKU")).toHaveAccessibleDescription(/El SKU no es válido/),
    );
    await waitFor(() => expect(field("Marca")).toHaveLength(2));
    fireEvent.change(field("Marca"), { target: { value: "b1" } });
    const lists = () => api.mock.calls.filter(([url]) => String(url).includes("limit=200"));
    await again(bad("brand_id"), () =>
      expect(alert()).toHaveTextContent("La marca elegida ya no está"),
    );
    await waitFor(() => expect(lists()).toHaveLength(4)); // marcas y categorías, otra vez
    await again(bad("category_id"), () =>
      expect(alert()).toHaveTextContent("La categoría elegida ya no"),
    );
    await again({ status: 403, body: { code: "PERMISSION_DENIED" } }, () =>
      expect(alert()).toHaveTextContent("No tienes permiso para crear productos."),
    );
    await again({ status: 500, body: {} }, () =>
      expect(alert()).toHaveTextContent("Algo salió mal"),
    );
  });

  it("si la lista no se puede releer tras crear, el aviso lo dice; sin marcas, se crea igual", async () => {
    let created = false;
    mockApi({
      [LIST]: () => (created ? { status: 500, body: {} } : page([])),
      [BRANDS]: { status: 500, body: {} },
      [CATEGORIES]: OPTIONS[CATEGORIES],
      [CREATE]: () => {
        created = true;
        return { status: 201, body: collar };
      },
    });
    screenOf(manager);
    await screen.findByText("Esta organización todavía no tiene productos.");
    fill({ Nombre: "Collar", SKU: "C1" });
    await waitFor(() =>
      expect(field("Marca")).toHaveAccessibleDescription(/No se pudo cargar la lista/),
    );
    expect(field("Categoría")).not.toHaveAccessibleDescription(/./);
    press("Crear producto", form());
    expect(
      await screen.findByText(
        "Producto «Collar» creado, pero la lista no se pudo actualizar. Recarga la página para verlo.",
      ),
    ).toBeVisible();
  });

  it("sin red crear falla y se dice, sin cola; Enter mantenido no reabre ni reenvía", async () => {
    const api = mockApi({ ...OPTIONS, [LIST]: page([]) });
    screenOf(manager);
    const open = await screen.findByRole("button", { name: "Crear producto" });
    expect(fireEvent.keyDown(open, { key: "Enter", repeat: true })).toBe(false);
    fill({ Nombre: "Collar" });
    press("Crear producto", form()); // sin SKU no se envía
    expect(field("SKU")).toHaveAccessibleDescription(/Escribe el SKU\./);
    expect(field("SKU")).toHaveFocus();
    fireEvent.change(field("SKU"), { target: { value: "C1" } });
    await waitFor(() => expect(field("Marca")).toHaveLength(2));
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    press("Crear producto", form());
    expect(await within(form()).findByRole("alert")).toHaveTextContent("No hay conexión");
    const held = within(form()).getByRole("button", { name: "Crear producto" });
    expect(fireEvent.keyDown(held, { key: "Enter", repeat: true })).toBe(false);
    expect(bodies(api)).toHaveLength(1);
  });
});
