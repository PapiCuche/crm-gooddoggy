import { onlineManager } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Category, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { CategoriesTree } from "./categories-tree";

const LIST = "GET /api/v1/o/acme/categories/?limit=200";
const CREATE = "POST /api/v1/o/acme/categories/";
const base: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "products.view", scopes: [] }],
};
const manager: SelfContext = {
  ...base,
  permissions: [...base.permissions, { code: "categories.manage", scopes: [] }],
};
const node = (id: string, name: string, path: string, parent_id: string | null = null) => ({
  id,
  name,
  path,
  parent_id,
  slug: path.split("/").at(-2)!,
  depth: path.split("/").length - 2,
});
const phones = node("c1", "Celulares", "/celulares/");
const iphone = node("c2", "iPhone", "/celulares/iphone/", "c1");
const pro = node("c4", "Pro", "/celulares/iphone/pro/", "c2");
const extras = node("c3", "Accesorios", "/accesorios/");
const cases = node("c5", "Fundas", "/accesorios/fundas/", "c3");
const silicone = node("c6", "Silicona", "/accesorios/fundas/silicona/", "c5");
const TREE = [phones, extras, iphone, cases, pro, silicone]; // por creación, no por rama
const page = (results: Category[]) => ({ status: 200, body: { results, next: null } });

const screenOf = (tenant: SelfContext) =>
  renderApp(
    <TenantProvider value={tenant}>
      <CategoriesTree />
    </TenantProvider>,
  );
const sent = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls
    .filter(([, init]) => (init?.method ?? "GET") === method)
    .map(([url, init]) => [String(url), init?.body ? JSON.parse(String(init.body)) : null]);
// Como una pulsación real: el botón recibe el foco antes del clic.
function press(name: string, within_: HTMLElement = document.body) {
  const button = within(within_).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
// Los nombres de las categorías de un nivel, sin las de dentro.
const names = (list: HTMLElement) =>
  within(list)
    .getAllByRole("listitem")
    .filter((item) => item.parentElement === list)
    .map((item) => item.querySelector(".font-medium")?.textContent);
const options = () =>
  [...(screen.getByLabelText("Dentro de") as HTMLSelectElement).options].map((option) => [
    option.text.trim(),
    option.disabled,
  ]);

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("CategoriesTree", () => {
  it("arma el árbol con todas las páginas, por rama; sin `categories.manage` no ofrece escribir", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: TREE.slice(0, 3), next: "p2" } },
      [`${LIST}&cursor=p2`]: page(TREE.slice(3)),
    });
    screenOf(base);
    expect(screen.getByRole("status")).toHaveTextContent("Cargando categorías");
    const tree = await screen.findByRole("list", { name: "Categorías" });
    expect(names(tree)).toEqual(["Accesorios", "Celulares"]);
    expect(names(screen.getByRole("list", { name: "Dentro de Celulares" }))).toEqual(["iPhone"]);
    const nested = within(screen.getByRole("list", { name: "Dentro de Celulares" }));
    expect(names(nested.getByRole("list", { name: "Dentro de iPhone" }))).toEqual(["Pro"]);
    expect(screen.getByText("silicona")).toHaveClass("font-mono");
    expect(screen.getByRole("status")).toHaveTextContent("2 categorías principales en el árbol");
    expect(sent(api, "GET")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Crear categoría" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Editar|Borrar/ })).toBeNull();
  });

  it("una hija cuya madre no llegó se enseña en la raíz, no se pierde", async () => {
    mockApi({ [LIST]: page([phones, pro]) }); // falta iPhone, la madre de Pro
    screenOf(base);
    expect(names(await screen.findByRole("list", { name: "Categorías" }))).toEqual([
      "Celulares",
      "Pro",
    ]);
  });

  it("un cursor repetido es un fallo de la API, no una lista sin fin; cada página lleva la señal", async () => {
    let turns = 0; // sin la guarda, la lista acabaría tras diez vueltas y no habría error
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [phones], next: "p2" } },
      [`${LIST}&cursor=p2`]: () => ({
        status: 200,
        body: { results: [extras], next: ++turns < 10 ? "p2" : null },
      }),
    });
    screenOf(base);
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal");
    expect(sent(api, "GET")).toHaveLength(4); // dos páginas, y otra vez en el único reintento
    for (const [, init] of api.mock.calls) expect(init?.signal?.aborted).toBe(false);
  });

  it("sin categorías lo dice; sin permiso, la API decide y la pantalla lo explica", async () => {
    mockApi({ [LIST]: page([]) });
    screenOf(base);
    expect(await screen.findByText("Esta organización todavía no tiene categorías.")).toBeVisible();
    vi.unstubAllGlobals();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf(base);
    expect(
      await screen.findByText("No tienes permiso para ver el catálogo de esta organización."),
    ).toBeVisible();
  });

  it("crea en la raíz y dentro de otra; el quinto nivel no admite hijas; un envío por pulsación", async () => {
    const deep = [1, 2, 3, 4, 5].map((n) =>
      node(
        `d${n}`,
        `N${n}`,
        `/${"abcde".slice(0, n).split("").join("/")}/`,
        n > 1 ? `d${n - 1}` : null,
      ),
    );
    let rows: Category[] = deep;
    const api = mockApi({
      [LIST]: () => page(rows),
      [CREATE]: () => {
        rows = [...deep, node("x1", "Perros", "/perros/")];
        return { status: 201, body: rows.at(-1) };
      },
    });
    screenOf(manager);
    await screen.findByRole("list", { name: "Categorías" });
    press("Crear categoría");
    const slug = screen.getByLabelText("Identificador (opcional)");
    expect(slug).toHaveAttribute("autocapitalize", "none");
    expect(options()).toEqual([
      ["En la raíz (sin categoría madre)", false],
      ["N1", false],
      ["N2, dentro de N1", false], // la madre se oye; la sangría, no
      ["N3, dentro de N2", false],
      ["N4, dentro de N3", false],
      ["N5, dentro de N4", true],
    ]);
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Perros" } });
    fireEvent.change(slug, { target: { value: "perros" } });
    press("Crear categoría", screen.getByRole("form", { name: "Nueva categoría" }));
    fireEvent.submit(screen.getByRole("form", { name: "Nueva categoría" })); // otra vez, enseguida
    expect(await screen.findByText("Categoría «Perros» creada.")).toBeVisible();
    expect(screen.getByText("perros")).toBeVisible(); // el árbol se volvió a pedir
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Crear categoría" })).toHaveFocus(),
    );
    press("Crear categoría");
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Collares" } });
    fireEvent.change(screen.getByLabelText("Dentro de"), { target: { value: "d4" } });
    press("Crear categoría", screen.getByRole("form", { name: "Nueva categoría" }));
    await waitFor(() => expect(sent(api, "POST")).toHaveLength(2));
    expect(sent(api, "POST").map(([, body]) => body)).toEqual([
      { name: "Perros", slug: "perros" },
      { name: "Collares", parent_id: "d4" },
    ]);
  });

  it("sigue «Creando…» hasta que vuelve el árbol; mientras, «Dentro de» no cambia; reabrir empieza de cero", async () => {
    let rows: Category[] = TREE;
    let release = () => {};
    const api = mockApi({
      [LIST]: () => page(rows),
      [CREATE]: () => {
        rows = [...TREE, node("x1", "Perros", "/celulares/perros/", "c1")];
        return { status: 201, body: rows.at(-1) };
      },
    });
    const reply = api.getMockImplementation()!;
    api.mockImplementation(async (input, init) => {
      // El árbol de después del alta tarda: se suelta a mano.
      if ((init?.method ?? "GET") === "GET" && rows !== TREE) {
        await new Promise<void>((resolve) => (release = resolve));
      }
      return reply(input, init);
    });
    screenOf(manager);
    await screen.findByRole("button", { name: "Crear categoría" });
    press("Crear categoría");
    const form = screen.getByRole("form", { name: "Nueva categoría" });
    fireEvent.change(within(form).getByLabelText("Nombre"), { target: { value: "Perros" } });
    fireEvent.change(within(form).getByLabelText("Dentro de"), { target: { value: "c1" } });
    press("Crear categoría", form);
    await waitFor(() => expect(sent(api, "GET")).toHaveLength(2)); // el árbol, otra vez
    expect(within(form).getByRole("button", { name: "Creando…" })).toBeVisible();
    fireEvent.change(within(form).getByLabelText("Dentro de"), { target: { value: "c3" } });
    expect(within(form).getByLabelText("Dentro de")).toHaveValue("c1");
    release();
    expect(await screen.findByText("Categoría «Perros» creada.")).toBeVisible();
    expect(
      within(screen.getByRole("list", { name: "Dentro de Celulares" })).getByText("Perros"),
    ).toBeVisible();
    press("Crear categoría");
    expect(screen.queryByText("Categoría «Perros» creada.")).toBeNull(); // el aviso ya no aplica
    expect(screen.getByLabelText("Dentro de")).toHaveValue("");
  });

  it("si el árbol no se puede releer tras crear, el aviso lo dice en vez de darlo por hecho", async () => {
    let created = false;
    mockApi({
      [LIST]: () => (created ? { status: 500, body: {} } : page(TREE)),
      [CREATE]: () => {
        created = true;
        return { status: 201, body: node("x1", "Perros", "/perros/") };
      },
    });
    screenOf(manager);
    await screen.findByRole("list", { name: "Categorías" });
    press("Crear categoría");
    const form = screen.getByRole("form", { name: "Nueva categoría" });
    fireEvent.change(within(form).getByLabelText("Nombre"), { target: { value: "Perros" } });
    press("Crear categoría", form);
    expect(
      await screen.findByText(
        "Categoría «Perros» creada, pero el árbol no se pudo actualizar. Recarga la página para verla.",
      ),
    ).toBeVisible();
    expect(screen.queryByText("Categoría «Perros» creada.")).toBeNull();
  });

  it("los errores del alta se dicen por `code`, junto al campo o al lugar", async () => {
    let reply = { status: 409, body: { code: "CATEGORY_SLUG_TAKEN" } as object };
    mockApi({ [LIST]: page(TREE), [CREATE]: () => reply });
    screenOf(manager);
    fireEvent.click(await screen.findByRole("button", { name: "Crear categoría" }));
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "iPhone" } });
    fireEvent.change(screen.getByLabelText("Dentro de"), { target: { value: "c1" } });
    press("Crear categoría", screen.getByRole("form", { name: "Nueva categoría" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Identificador (opcional)")).toHaveAccessibleDescription(
        /identificador que se forma con este nombre/,
      ),
    );
    reply = { status: 400, body: { code: "VALIDATION_ERROR", fields: { parent_id: ["x"] } } };
    press("Crear categoría", screen.getByRole("form", { name: "Nueva categoría" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se puede poner ahí");
  });

  it("sin red crear falla y se dice, sin cola; Enter mantenido no reabre ni reenvía", async () => {
    const api = mockApi({ [LIST]: page(TREE) });
    screenOf(manager);
    const open = await screen.findByRole("button", { name: "Crear categoría" });
    expect(fireEvent.keyDown(open, { key: "Enter", repeat: true })).toBe(false);
    fireEvent.click(open);
    const form = screen.getByRole("form", { name: "Nueva categoría" });
    fireEvent.change(within(form).getByLabelText("Nombre"), { target: { value: "Perros" } });
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    press("Crear categoría", form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("No hay conexión");
    const held = within(form).getByRole("button", { name: "Crear categoría" });
    expect(fireEvent.keyDown(held, { key: "Enter", repeat: true })).toBe(false);
    expect(sent(api, "POST")).toHaveLength(1);
  });
});
