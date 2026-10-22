import { onlineManager } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Category, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { CategoriesTree } from "./categories-tree";

const LIST = "GET /api/v1/o/acme/categories/?limit=200";
const CREATE = "POST /api/v1/o/acme/categories/";
const ONE = (id: string) => `/api/v1/o/acme/categories/${id}/`;
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

  it("renombra sin mover: solo envía el nombre, cambia la fila sin releer y el foco vuelve a «Editar»", async () => {
    const api = mockApi({
      [LIST]: page(TREE),
      [`PATCH ${ONE("c2")}`]: { status: 200, body: { ...iphone, name: "iPhone 17" } },
    });
    screenOf(manager);
    press("Editar iPhone", await screen.findByRole("list", { name: "Categorías" }));
    const form = screen.getByRole("form", { name: "Editar iPhone" });
    expect(within(form).getByLabelText("Nombre")).toHaveValue("iPhone");
    expect(within(form).getByLabelText("Dentro de")).toHaveValue("c1");
    fireEvent.change(within(form).getByLabelText("Nombre"), { target: { value: "iPhone 17" } });
    press("Guardar", form);
    fireEvent.submit(form); // otra vez, enseguida
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Editar iPhone 17" })).toHaveFocus(),
    );
    expect(screen.getByText("Categoría renombrada: «iPhone 17».")).toBeVisible();
    expect(sent(api, "PATCH")).toEqual([[ONE("c2"), { name: "iPhone 17" }]]);
    expect(sent(api, "GET")).toHaveLength(1);
  });

  it("mueve la rama: ni dentro de sí ni donde pasaría de 5 niveles; el árbol vuelve y el foco va al título", async () => {
    let rows: Category[] = TREE;
    const api = mockApi({
      [LIST]: () => page(rows),
      [`PATCH ${ONE("c1")}`]: () => {
        const moved = {
          ...phones,
          parent_id: "c5",
          depth: 3,
          path: "/accesorios/fundas/celulares/",
        };
        rows = [extras, cases, silicone, moved];
        return { status: 200, body: moved };
      },
    });
    screenOf(manager);
    press("Editar Celulares", await screen.findByRole("list", { name: "Categorías" }));
    expect(options()).toEqual([
      ["En la raíz (sin categoría madre)", false],
      ["Accesorios", false],
      ["Fundas, dentro de Accesorios", false], // 2 + 3 niveles de la rama: cabe justo
      ["Silicona, dentro de Fundas", true], // 3 + 3: pasaría de 5
      ["Celulares", true], // ella misma
      ["iPhone, dentro de Celulares", true], // y sus descendientes
      ["Pro, dentro de iPhone", true],
    ]);
    fireEvent.change(screen.getByLabelText("Dentro de"), { target: { value: "c5" } });
    press("Guardar", screen.getByRole("form", { name: "Editar Celulares" }));
    expect(
      await screen.findByText("Categoría «Celulares» movida con sus subcategorías."),
    ).toBeVisible();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    const fundas = screen.getByRole("list", { name: "Dentro de Fundas" });
    expect(names(fundas)).toEqual(["Celulares", "Silicona"]);
    expect(sent(api, "PATCH")).toEqual([[ONE("c1"), { parent_id: "c5" }]]);
  });

  it("a la raíz envía `parent_id: null`; sin cambios no envía nada", async () => {
    const api = mockApi({
      [LIST]: page(TREE),
      [`PATCH ${ONE("c2")}`]: { status: 200, body: { ...iphone, parent_id: null } },
    });
    screenOf(manager);
    press("Editar iPhone", await screen.findByRole("list", { name: "Categorías" }));
    press("Guardar", screen.getByRole("form")); // nada cambió: se cierra sin pedir
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Editar iPhone" })).toHaveFocus(),
    );
    press("Editar iPhone");
    fireEvent.change(screen.getByLabelText("Dentro de"), { target: { value: "" } });
    press("Guardar", screen.getByRole("form"));
    expect(await screen.findByText(/movida con sus subcategorías/)).toBeVisible();
    expect(sent(api, "PATCH")).toEqual([[ONE("c2"), { parent_id: null }]]);
  });

  it("si el árbol no se puede releer tras mover, el aviso lo dice", async () => {
    let moved = false;
    mockApi({
      [LIST]: () => (moved ? { status: 500, body: {} } : page(TREE)),
      [`PATCH ${ONE("c2")}`]: () => {
        moved = true;
        return { status: 200, body: { ...iphone, parent_id: null } };
      },
    });
    screenOf(manager);
    press("Editar iPhone", await screen.findByRole("list", { name: "Categorías" }));
    fireEvent.change(screen.getByLabelText("Dentro de"), { target: { value: "" } });
    press("Guardar", screen.getByRole("form"));
    expect(await screen.findByText(/movida, pero el árbol no se pudo actualizar/)).toBeVisible();
    expect(screen.queryByText(/movida con sus subcategorías/)).toBeNull();
    // La fila no se fue: el foco vuelve a su «Editar», no al título.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Editar iPhone" })).toHaveFocus(),
    );
  });

  it("al editar, los errores van por `code` en el formulario", async () => {
    let reply = { status: 409, body: { code: "CATEGORY_SLUG_TAKEN" } as object };
    mockApi({ [LIST]: page(TREE), [`PATCH ${ONE("c2")}`]: () => reply });
    screenOf(manager);
    press("Editar iPhone", await screen.findByRole("list", { name: "Categorías" }));
    const form = screen.getByRole("form", { name: "Editar iPhone" });
    fireEvent.change(screen.getByLabelText("Dentro de"), { target: { value: "c3" } });
    press("Guardar", form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("mismo identificador");
    for (const [next, text] of [
      [
        { status: 400, body: { code: "VALIDATION_ERROR", fields: { parent_id: ["x"] } } },
        "No se puede poner ahí",
      ],
      [
        { status: 403, body: { code: "PERMISSION_DENIED" } },
        "No tienes permiso para cambiar categorías.",
      ],
      [{ status: 500, body: { code: "INTERNAL_ERROR" } }, "Algo salió mal"],
    ] as const) {
      reply = next;
      press("Guardar", form);
      await waitFor(() => expect(within(form).getByRole("alert")).toHaveTextContent(text));
    }
  });

  it("borrar pide confirmación con el foco en «Cancelar» y explica por qué no se pudo", async () => {
    let reply = { status: 409, body: { code: "CATEGORY_HAS_CHILDREN" } as object };
    const api = mockApi({ [LIST]: page(TREE), [`DELETE ${ONE("c1")}`]: () => reply });
    screenOf(manager);
    press("Borrar Celulares", await screen.findByRole("list", { name: "Categorías" }));
    const ask = screen.getByRole("group", { name: "Borrar Celulares" });
    expect(ask).toHaveAccessibleDescription(/si no tiene subcategorías ni productos/);
    await waitFor(() =>
      expect(within(ask).getByRole("button", { name: "Cancelar" })).toHaveFocus(),
    );
    press("Cancelar", ask);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Borrar Celulares" })).toHaveFocus(),
    );
    expect(sent(api, "DELETE")).toHaveLength(0);
    press("Borrar Celulares");
    press("Borrar", screen.getByRole("group"));
    expect(await screen.findByRole("alert")).toHaveTextContent("tiene subcategorías");
    await waitFor(() => expect(sent(api, "GET")).toHaveLength(2)); // por si no las enseñaba
    for (const [next, text] of [
      [{ status: 409, body: { code: "CATEGORY_IN_USE" } }, "Hay productos en esta categoría"],
      [{ status: 403, body: { code: "PERMISSION_DENIED" } }, "No tienes permiso para borrar"],
      [{ status: 400, body: { code: "VALIDATION_ERROR" } }, "Algo salió mal"],
    ] as const) {
      reply = next;
      press("Borrar", screen.getByRole("group"));
      await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(text));
    }
    expect(sent(api, "GET")).toHaveLength(2);
  });

  it("una lectura del árbol en vuelo se cancela al borrar y se repite después", async () => {
    let rows: Category[] = TREE;
    let deletes = 0;
    let release = () => {};
    const api = mockApi({
      [LIST]: () => page(rows),
      [`DELETE ${ONE("c4")}`]: () => {
        if (++deletes === 1) return { status: 409, body: { code: "CATEGORY_HAS_CHILDREN" } };
        rows = [...TREE.filter((row) => row.id !== "c4"), node("x1", "Perros", "/perros/")];
        return { status: 204 };
      },
    });
    const reply = api.getMockImplementation()!;
    api.mockImplementation(async (input, init) => {
      // La relectura que pide el 409 se queda en vuelo, con el árbol de antes.
      if (deletes === 1 && (init?.method ?? "GET") === "GET") {
        await new Promise<void>((resolve) => (release = resolve));
      }
      return reply(input, init);
    });
    screenOf(manager);
    press("Borrar Pro", await screen.findByRole("list", { name: "Categorías" }));
    press("Borrar", screen.getByRole("group"));
    await waitFor(() => expect(sent(api, "GET")).toHaveLength(2));
    press("Borrar", screen.getByRole("group"));
    expect(await screen.findByText("Categoría «Pro» borrada.")).toBeVisible();
    release(); // llega tarde y con Pro: ya no cuenta
    expect(await screen.findByText("Perros")).toBeVisible(); // se repitió la lectura
    expect(screen.queryByText("Pro")).toBeNull();
  });

  it("borra una vez aunque se pulse dos; la fila sale sin releer y el foco va al título", async () => {
    const api = mockApi({ [LIST]: page(TREE), [`DELETE ${ONE("c4")}`]: { status: 204 } });
    screenOf(manager);
    press("Borrar Pro", await screen.findByRole("list", { name: "Categorías" }));
    const ask = screen.getByRole("group", { name: "Borrar Pro" });
    press("Borrar", ask);
    fireEvent.click(within(ask).getByRole("button", { name: /Borra/ }));
    expect(await screen.findByText("Categoría «Pro» borrada.")).toBeVisible();
    expect(screen.queryByRole("list", { name: "Dentro de iPhone" })).toBeNull();
    expect(screen.getByText("iPhone")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    expect(sent(api, "DELETE")).toHaveLength(1);
    expect(sent(api, "GET")).toHaveLength(1);
  });

  it("si la categoría ya no existe, el árbol avisa y se vuelve a pedir; sin sesión no hay error", async () => {
    let rows = TREE;
    const gone = { status: 404, body: { code: "NOT_FOUND" } };
    mockApi({
      [LIST]: () => page(rows),
      [`DELETE ${ONE("c4")}`]: () => {
        rows = TREE.filter((row) => row.id !== "c4");
        return gone;
      },
      [`PATCH ${ONE("c2")}`]: () => {
        rows = [phones, extras, cases, silicone];
        return gone;
      },
      [`DELETE ${ONE("c3")}`]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    screenOf(manager);
    press("Borrar Pro", await screen.findByRole("list", { name: "Categorías" }));
    press("Borrar", screen.getByRole("group"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "«Pro» ya no está en el catálogo: el árbol se actualizó.",
    );
    await waitFor(() => expect(screen.queryByText("Pro")).toBeNull());
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    press("Editar iPhone");
    expect(screen.queryByRole("alert")).toBeNull(); // abrir retira el aviso anterior
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "iPhone 17" } });
    press("Guardar", screen.getByRole("form"));
    expect(await screen.findByRole("alert")).toHaveTextContent("«iPhone» ya no está");
    await waitFor(() => expect(screen.queryByText("iPhone")).toBeNull());
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    press("Borrar Accesorios");
    press("Borrar", screen.getByRole("group"));
    await waitFor(() =>
      expect(
        within(screen.getByRole("group")).getByRole("button", { name: "Borrando…" }),
      ).toBeVisible(),
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("sin red editar o borrar falla y se dice, sin cola; Enter mantenido no reabre", async () => {
    const api = mockApi({ [LIST]: page(TREE) });
    screenOf(manager);
    const edit = await screen.findByRole("button", { name: "Editar Pro" });
    expect(fireEvent.keyDown(edit, { key: "Enter", repeat: true })).toBe(false);
    const remove = screen.getByRole("button", { name: "Borrar Pro" });
    expect(fireEvent.keyDown(remove, { key: "Enter", repeat: true })).toBe(false);
    press("Editar Pro");
    const form = screen.getByRole("form", { name: "Editar Pro" });
    fireEvent.change(within(form).getByLabelText("Nombre"), { target: { value: "Pro Max" } });
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    press("Guardar", form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("No hay conexión");
    press("Cancelar", form);
    press("Borrar Pro");
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    press("Borrar", screen.getByRole("group"));
    expect(await within(screen.getByRole("group")).findByRole("alert")).toHaveTextContent(
      "No hay conexión",
    );
    expect(sent(api, "PATCH")).toHaveLength(1);
    expect(sent(api, "DELETE")).toHaveLength(1);
  });
});
