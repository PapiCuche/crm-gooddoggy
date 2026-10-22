import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Brand, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { BrandsList } from "./brands-list";

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
const deletes = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.filter(([, init]) => init?.method === "DELETE");

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => vi.unstubAllGlobals());

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
    const usual = api.getMockImplementation()!;
    api.mockImplementation((input, init) =>
      init?.method === "DELETE" ? Promise.reject(new TypeError("offline")) : usual(input, init),
    );
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Borrar Apple" }));
    fireEvent.click(screen.getByRole("button", { name: "Borrar marca" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.getByText("Apple")).toBeVisible();
  });
});
