import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
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
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
// Como una pulsación real: el botón recibe el foco antes del clic.
function press(name: string) {
  const button = screen.getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
async function renaming(name = "Apple") {
  fireEvent.click(await screen.findByRole("button", { name: `Renombrar ${name}` }));
  return screen.getByRole("form", { name: `Renombrar ${name}` });
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

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

  it("anuncia el nombre que guardó la API y devuelve el foco a «Crear marca»", async () => {
    mockApi({
      [LIST]: page([]),
      [CREATE]: { status: 201, body: { id: "b9", name: "Acme Perú", slug: "acme-peru" } },
    });
    screenOf(manager);
    fireEvent.click(await screen.findByRole("button", { name: "Crear marca" }));
    const slugInput = screen.getByLabelText("Identificador (opcional)");
    expect(slugInput).toHaveAttribute("autocapitalize", "none"); // el móvil no pone «Apple»
    expect(slugInput).toHaveAttribute("spellcheck", "false");
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "acme perú" } });
    press("Crear marca");
    expect(await screen.findByText("Marca «Acme Perú» creada.")).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Crear marca" })).toHaveFocus());
  });

  it("con el slug vacío, un 400 o un 409 del slug hablan del que se forma con el nombre", async () => {
    let create: Reply = {
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { slug: ["No se puede formar."] } },
    };
    const api = mockApi({ [LIST]: page([]), [CREATE]: () => create });
    screenOf(manager);
    fireEvent.click(await screen.findByRole("button", { name: "Crear marca" }));
    const slugInput = screen.getByLabelText("Identificador (opcional)");
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "東京" } });
    press("Crear marca");
    await waitFor(() =>
      expect(slugInput).toHaveAccessibleDescription(
        "Con este nombre no se puede formar un identificador: escríbelo tú.",
      ),
    );
    expect(slugInput).toHaveFocus();
    create = { status: 409, body: { code: "BRAND_SLUG_TAKEN" } };
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Apple" } });
    press("Crear marca");
    await waitFor(() =>
      expect(slugInput).toHaveAccessibleDescription(
        /el identificador que se forma con este nombre/,
      ),
    );
    // Con un slug escrito, el 400 habla de lo escrito.
    create = { status: 400, body: { code: "VALIDATION_ERROR", fields: { slug: ["Mal."] } } };
    fireEvent.change(slugInput, { target: { value: "Apple" } });
    press("Crear marca");
    await waitFor(() =>
      expect(slugInput).toHaveAccessibleDescription(/El identificador no es válido/),
    );
    expect(posts(api, "POST")).toHaveLength(3);
  });

  it("sin red crear o renombrar falla y se dice: no queda en cola", async () => {
    const api = mockApi({ [LIST]: page([apple]) });
    screenOf(manager);
    const form = await renaming();
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    press("Guardar");
    expect(await within(form).findByRole("alert")).toHaveTextContent("No hay conexión");
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    fireEvent.click(screen.getByRole("button", { name: "Crear marca" }));
    fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Sony" } });
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    press("Crear marca");
    expect(
      await within(screen.getByRole("form", { name: "Nueva marca" })).findByRole("alert"),
    ).toHaveTextContent("No hay conexión");
    expect(posts(api, "PATCH")).toHaveLength(1);
    expect(posts(api, "POST")).toHaveLength(1);
  });

  it("renombrar lo anuncia, devuelve el foco y abre el formulario a lo ancho de la fila", async () => {
    mockApi({
      [LIST]: page([apple]),
      [RENAME("b1")]: { status: 200, body: { ...apple, name: "Apple Inc." } },
    });
    screenOf(manager);
    const form = await renaming();
    expect(form.closest("li > div")).toHaveClass("sm:has-[form]:col-span-2");
    fireEvent.change(within(form).getByLabelText("Nombre"), { target: { value: "Apple Inc." } });
    press("Guardar");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Renombrar Apple Inc." })).toHaveFocus(),
    );
    expect(screen.getByText("Marca renombrada: «Apple Inc.».")).toBeInTheDocument();
  });

  it("una relectura en vuelo no pisa lo renombrado: se cancela y se repite", async () => {
    const saved = { ...apple, name: "Apple Inc." };
    let rows = [apple];
    const api = mockApi({ [LIST]: () => page(rows), [RENAME("b1")]: { status: 200, body: saved } });
    const view = screenOf(manager);
    const form = await renaming();
    fireEvent.change(within(form).getByLabelText("Nombre"), { target: { value: "Apple Inc." } });
    let releaseRead = () => {};
    api.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (releaseRead = resolve));
      return new Response(JSON.stringify({ results: [apple], next: null }), { status: 200 });
    });
    void view.client.refetchQueries();
    await tick();
    rows = [saved];
    press("Guardar");
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    releaseRead(); // llega tarde con «Apple»: no vuelve a la fila
    await waitFor(() => expect(posts(api, "GET")).toHaveLength(3));
    await tick();
    expect(screen.getByText("Apple Inc.")).toBeVisible();
    expect(screen.queryByText("Apple")).not.toBeInTheDocument();
  });

  it("con «Cargar más» en vuelo: se cancela, no pisa lo renombrado y no se repite", async () => {
    const sony: Brand = { id: "b2", name: "Sony", slug: "sony" };
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [apple], next: "abc" } },
      [`${LIST}?cursor=abc`]: page([sony]),
      [RENAME("b1")]: { status: 200, body: { ...apple, name: "Apple Inc." } },
    });
    screenOf(manager);
    const form = await renaming();
    fireEvent.change(within(form).getByLabelText("Nombre"), { target: { value: "Apple Inc." } });
    const answer = api.getMockImplementation()!;
    let release = () => {};
    api.mockImplementationOnce(async (...request) => {
      await new Promise<void>((resolve) => (release = resolve));
      return answer(...request);
    });
    const more = screen.getByRole("button", { name: "Cargar más" });
    fireEvent.click(more); // la página 2 sale con la página 1 de antes de renombrar
    await waitFor(() => expect(posts(api, "GET")).toHaveLength(2));
    press("Guardar");
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    release();
    await tick();
    expect(screen.getByText("Apple Inc.")).toBeVisible();
    expect(screen.queryByText("Sony")).not.toBeInTheDocument(); // cancelada: hay que pedirla otra vez
    expect(posts(api, "GET")).toHaveLength(2);
  });

  it("si la API responde con otra marca, no se da por renombrada y la lista se vuelve a pedir", async () => {
    const api = mockApi({
      [LIST]: page([apple]),
      [RENAME("b1")]: { status: 200, body: { id: "b2", name: "Zeta", slug: "zeta" } },
    });
    screenOf(manager);
    const form = await renaming();
    press("Guardar");
    expect(await within(form).findByRole("alert")).toHaveTextContent("Algo salió mal");
    await waitFor(() => expect(posts(api, "GET")).toHaveLength(2));
    expect(screen.queryByText("Zeta")).not.toBeInTheDocument();
  });

  it("un 404 con el foco en ninguna parte lo lleva al título", async () => {
    mockApi({
      [LIST]: page([apple]),
      [RENAME("b1")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    screenOf(manager);
    const form = await renaming();
    (document.activeElement as HTMLElement).blur();
    fireEvent.submit(form);
    expect(await screen.findByRole("alert")).toHaveTextContent("«Apple» ya no está");
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
  });

  it("Enter mantenido no vuelve a pulsar: ni reabre ni reenvía", async () => {
    const api = mockApi({
      [LIST]: page([apple]),
      [CREATE]: { status: 500, body: { code: "X" } },
      [RENAME("b1")]: { status: 500, body: { code: "X" } },
    });
    screenOf(manager);
    const open = await screen.findByRole("button", { name: "Crear marca" });
    expect(fireEvent.keyDown(open, { key: "Enter", repeat: true })).toBe(false);
    const rename = screen.getByRole("button", { name: "Renombrar Apple" });
    expect(fireEvent.keyDown(rename, { key: "Enter", repeat: true })).toBe(false);
    const form = await renaming();
    press("Guardar");
    await within(form).findByRole("alert");
    const held = within(form).getByRole("button", { name: "Guardar" });
    expect(fireEvent.keyDown(held, { key: "Enter", repeat: true })).toBe(false);
    expect(posts(api, "PATCH")).toHaveLength(1);
  });
});
