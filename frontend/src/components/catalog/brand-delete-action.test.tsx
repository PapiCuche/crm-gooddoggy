import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Brand, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { BrandsList } from "./brands-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/brands/";
const DELETE = (id: string) => `DELETE /api/v1/o/acme/brands/${id}/`;
const manager: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [
    { code: "products.view", scopes: [] },
    { code: "brands.manage", scopes: [] },
  ],
};
const apple: Brand = { id: "b1", name: "Apple", slug: "apple" };
const samsung: Brand = { id: "b2", name: "Samsung", slug: "samsung" };
const page = (results: Brand[]) => ({ status: 200, body: { results, next: null } });

const screenOf = () =>
  renderApp(
    <TenantProvider value={manager}>
      <BrandsList />
    </TenantProvider>,
  );
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const deletes = (api: ReturnType<typeof mockApi>) => calls(api, "DELETE");
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
async function ask(name = "Apple") {
  fireEvent.click(await screen.findByRole("button", { name: `Borrar ${name}` }));
  return screen.getByRole("group", { name: `Borrar ${name}` });
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("Borrar una marca", () => {
  it("pide confirmación, con el foco en Cancelar, y cancelar no envía nada", async () => {
    const api = mockApi({ [LIST]: page([apple]) });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Borrar Apple" }));
    const group = screen.getByRole("group", { name: "Borrar Apple" });
    expect(group).toHaveAccessibleDescription(/su identificador «apple» quedará libre/);
    expect(screen.getByRole("button", { name: "Cancelar" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Cancelar" }));
    expect(screen.getByRole("button", { name: "Borrar Apple" })).toHaveFocus();
    expect(deletes(api)).toHaveLength(0);
  });

  it("al confirmar envía un solo DELETE, quita la fila y lo anuncia con el foco en el título", async () => {
    let listed = [apple, samsung];
    const api = mockApi({
      [LIST]: () => page(listed),
      [DELETE("b1")]: () => {
        listed = [samsung];
        return { status: 204 };
      },
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Borrar Apple" }));
    const confirm = screen.getByRole("button", { name: "Borrar marca" });
    fireEvent.click(confirm);
    fireEvent.click(confirm); // dos pulsaciones, una petición
    await waitFor(() => expect(screen.queryByText("Apple")).toBeNull());
    expect(deletes(api)).toHaveLength(1);
    expect(screen.getByText("Marca «Apple» borrada.")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("heading", { name: "Marcas" })).toHaveFocus();
    expect(screen.getByText("Samsung")).toBeVisible();
  });

  it("si ya no existe (404) la lista avisa y se vuelve a pedir; sin permiso lo dice", async () => {
    let listed = [apple, samsung];
    mockApi({
      [LIST]: () => page(listed),
      [DELETE("b1")]: () => {
        listed = [samsung];
        return { status: 404, body: { code: "NOT_FOUND" } };
      },
      [DELETE("b2")]: { status: 403, body: { code: "PERMISSION_DENIED" } },
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Borrar Apple" }));
    fireEvent.click(screen.getByRole("button", { name: "Borrar marca" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "«Apple» ya no está en el catálogo: la lista se actualizó.",
    );
    await waitFor(() => expect(screen.queryByText("Apple")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Borrar Samsung" }));
    fireEvent.click(screen.getByRole("button", { name: "Borrar marca" }));
    expect(await screen.findByText("No tienes permiso para borrar marcas.")).toBeVisible();
    expect(screen.getByText("Samsung")).toBeVisible(); // nada cambia
  });

  it("sin red falla y lo dice; no queda en cola", async () => {
    const api = mockApi({ [LIST]: page([apple]) });
    screenOf();
    const group = await ask();
    onlineManager.setOnline(false); // un borrado no espera en cola a que vuelva la red
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(within(group).getByRole("button", { name: "Borrar marca" }));
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(deletes(api)).toHaveLength(1);
    expect(screen.getByText("Apple")).toBeVisible();
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    expect(within(await ask()).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("tras un fallo el mismo botón vuelve a enviar", async () => {
    let reply: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    const api = mockApi({ [LIST]: page([apple, samsung]), [DELETE("b1")]: () => reply });
    screenOf();
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Borrar marca" });
    fireEvent.click(confirm);
    expect(await within(group).findByRole("alert")).toHaveTextContent("Algo salió mal");
    reply = { status: 204 };
    fireEvent.click(confirm);
    await waitFor(() => expect(screen.queryByText("Apple")).toBeNull());
    expect(deletes(api)).toHaveLength(2);
  });

  it("sin sesión no enseña un error, va al login una vez y no se puede enviar de nuevo", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/catalogo/marcas", search: "", assign });
    const api = mockApi({
      [LIST]: page([apple]),
      [DELETE("b1")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>
          <TenantProvider value={manager}>
            <BrandsList />
          </TenantProvider>
        </Providers>
      </NextIntlClientProvider>,
    );
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Borrar marca" });
    fireEvent.click(confirm);
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    await tick();
    fireEvent.click(confirm); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(deletes(api)).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Borrando…" })).toBe(confirm); // sigue abierta
    expect(screen.getByText("Apple")).toBeVisible();
  });

  it("con el foco ya en otra parte, borrar o un 404 cierran sin quitárselo", async () => {
    const api = mockApi({
      [LIST]: page([apple, samsung]),
      [DELETE("b1")]: { status: 204 },
      [DELETE("b2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    screenOf();
    let group = await ask("Samsung");
    let release = hold(api);
    fireEvent.click(within(group).getByRole("button", { name: "Borrar marca" }));
    await waitFor(() => expect(deletes(api)).toHaveLength(1));
    let elsewhere = screen.getByRole("button", { name: "Crear marca" });
    elsewhere.focus();
    release();
    expect(await screen.findByRole("alert")).toHaveTextContent("«Samsung» ya no está");
    await tick();
    expect(screen.queryByRole("group")).not.toBeInTheDocument(); // se cierra sin esperar
    expect(elsewhere).toHaveFocus();
    group = await ask();
    release = hold(api);
    fireEvent.click(within(group).getByRole("button", { name: "Borrar marca" }));
    await waitFor(() => expect(deletes(api)).toHaveLength(2));
    elsewhere = screen.getByRole("button", { name: "Renombrar Samsung" });
    elsewhere.focus();
    release();
    await waitFor(() => expect(screen.queryByText("Apple")).toBeNull());
    expect(elsewhere).toHaveFocus();
  });

  it("una relectura en vuelo no devuelve la fila borrada: se cancela y se repite", async () => {
    let listed = [apple, samsung];
    const api = mockApi({ [LIST]: () => page(listed), [DELETE("b1")]: { status: 204 } });
    const view = screenOf();
    const group = await ask();
    let releaseRead = () => {};
    api.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (releaseRead = resolve));
      return new Response(JSON.stringify({ results: [apple, samsung], next: null }), {
        status: 200,
      });
    });
    void view.client.refetchQueries();
    await tick();
    listed = [samsung];
    fireEvent.click(within(group).getByRole("button", { name: "Borrar marca" }));
    await waitFor(() => expect(screen.queryByText("Apple")).toBeNull());
    releaseRead(); // llega tarde con «Apple»: no vuelve
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3));
    await tick();
    expect(screen.queryByText("Apple")).toBeNull();
    expect(screen.getByText("Samsung")).toBeVisible();
  });

  it("borra de cualquier página, y «Cargar más» en vuelo no devuelve la fila", async () => {
    const sony: Brand = { id: "b3", name: "Sony", slug: "sony" };
    const lg: Brand = { id: "b4", name: "LG", slug: "lg" };
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [apple], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [sony], next: "def" } },
      [`${LIST}?cursor=def`]: page([lg]),
      [DELETE("b1")]: { status: 204 },
      [DELETE("b3")]: { status: 204 },
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    const group = await ask("Sony"); // en la página 2
    fireEvent.click(within(group).getByRole("button", { name: "Borrar marca" }));
    await waitFor(() => expect(screen.queryByText("Sony")).toBeNull());
    expect(screen.getByText("1 marca en la lista")).toBeInTheDocument(); // la cuenta, también
    const release = hold(api);
    const more = screen.getByRole("button", { name: "Cargar más" });
    fireEvent.click(more); // sale con las páginas de antes del borrado
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3));
    fireEvent.click(within(await ask()).getByRole("button", { name: "Borrar marca" }));
    await waitFor(() => expect(screen.queryByText("Apple")).toBeNull());
    release();
    await tick();
    expect(screen.queryByText("Apple")).toBeNull();
    expect(screen.queryByText("Sony")).toBeNull();
  });

  it("Enter mantenido no vuelve a pulsar: ni reabre ni reenvía", async () => {
    const api = mockApi({ [LIST]: page([apple]), [DELETE("b1")]: { status: 500, body: {} } });
    screenOf();
    const open = await screen.findByRole("button", { name: "Borrar Apple" });
    expect(fireEvent.keyDown(open, { key: "Enter", repeat: true })).toBe(false);
    const group = await ask();
    const confirm = within(group).getByRole("button", { name: "Borrar marca" });
    fireEvent.click(confirm);
    await within(group).findByRole("alert");
    expect(fireEvent.keyDown(confirm, { key: "Enter", repeat: true })).toBe(false);
    expect(deletes(api)).toHaveLength(1);
  });
});
