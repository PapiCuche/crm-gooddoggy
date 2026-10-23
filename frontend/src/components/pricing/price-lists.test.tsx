import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { PriceList, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { PriceLists } from "./price-lists";

const LIST = "GET /api/v1/o/acme/pricing/price-lists/";
const CREATE = "POST /api/v1/o/acme/pricing/price-lists/";
const PATCH = "PATCH /api/v1/o/acme/pricing/price-lists/p1/";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const seller: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("prices.view"),
};
const owner = { ...seller, permissions: grants("prices.view", "prices.manage") };
const regular: PriceList = {
  ...{ id: "p1", code: "REGULAR", name: "Regular", kind: "RETAIL", currency: "PEN" },
  ...{ is_default: true, required_permission: "prices.view", ai_exposable: false },
  ...{ payment_condition: null, is_active: true, sort_order: 0 },
};
const wholesale: PriceList = {
  ...{ ...regular, id: "p2", code: "MAYORISTA", name: "Mayorista", kind: "WHOLESALE" },
  ...{ is_default: false, required_permission: "prices.view_wholesale" },
  ...{ payment_condition: "BANK_TRANSFER", is_active: false, sort_order: 20 },
};
type Reply = { status: number; body: object };
const page = (results: unknown[], next: string | null = null): Reply => ({
  status: 200,
  body: { results, next },
});
const bad = (name: string): Reply => ({
  status: 400,
  body: { code: "VALIDATION_ERROR", fields: { [name]: ["x"] } },
});
const screenOf = (tenant: SelfContext) =>
  renderApp(
    <TenantProvider value={tenant}>
      <PriceLists />
    </TenantProvider>,
  );
const sent = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls
    .filter(([, init]) => init?.method === method)
    .map(([, init]) => JSON.parse(String(init?.body)));
const form = () => screen.getByRole("form");
const field = (label: string | RegExp) => within(form()).getByLabelText(label);
const alert = () => within(form()).getByRole("alert");
function press(name: string, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus(); // como una pulsación real: el botón recibe el foco antes del clic
  fireEvent.click(button);
}
const fill = (values: Record<string, string>) => {
  for (const [label, value] of Object.entries(values)) {
    fireEvent.change(field(label), { target: { value } });
  }
};

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => vi.unstubAllGlobals());

describe("PriceLists", () => {
  it("enseña cada lista con tipo, moneda, quién la ve, predeterminada, estado y orden; sin `prices.manage`, sin botones", async () => {
    mockApi({ [LIST]: page([regular, wholesale]) });
    screenOf(seller);
    const list = await screen.findByRole("list", { name: "Listas de precios" });
    const [first, second] = within(list).getAllByRole("listitem");
    expect(within(first!).getByText("REGULAR")).toHaveClass("font-mono");
    expect(first).toHaveTextContent("Minorista · PEN · Sin condición de pago");
    expect(within(first!).getByText("Quién la ve: Todos")).toBeVisible();
    expect(within(first!).getByText("Predeterminada")).toBeVisible();
    expect(within(first!).getByText("Activa")).toHaveClass("text-success");
    expect(within(first!).getByText("Orden 0")).toBeVisible();
    expect(second).toHaveTextContent("Mayorista · PEN · Transferencia bancaria");
    expect(within(second!).getByText("Quién la ve: Mayoristas")).toBeVisible();
    expect(within(second!).queryByText("Predeterminada")).toBeNull();
    expect(within(second!).getByText("Inactiva")).toHaveClass("text-muted");
    expect(within(second!).getByText("Orden 20")).toBeVisible();
    expect(screen.getByText("2 listas en la vista")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("«Cargar más» trae la página siguiente con su cursor", async () => {
    const api = mockApi({
      [LIST]: page([regular], "abc"),
      [`${LIST}?cursor=abc`]: page([wholesale]),
    });
    screenOf(owner);
    await screen.findByText("Regular");
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("Mayorista")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Cargar más" })).toBeNull();
    expect(api).toHaveBeenCalledTimes(2);
  });

  it("vacía, sin permiso (403) y sin sesión (401) como las demás listas", async () => {
    mockApi({ [LIST]: page([]) });
    const empty = screenOf(owner);
    expect(await screen.findByText(/todavía no tiene listas de precios/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Crear lista" })).toBeVisible();
    empty.unmount();
    vi.unstubAllGlobals();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    const denied = screenOf(seller);
    expect(await screen.findByText(/No tienes permiso para ver las listas/)).toBeVisible();
    denied.unmount();
    vi.unstubAllGlobals();
    const api = mockApi({ [LIST]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } });
    screenOf(seller);
    await waitFor(() => expect(api).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("status")).toHaveTextContent("Cargando listas de precios…");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("crea: quién la ve sale del tipo hasta elegirlo; sin elegir, decide la API; un envío por pulsación", async () => {
    let rows: PriceList[] = [];
    const api = mockApi({
      [LIST]: () => page(rows),
      [CREATE]: () => {
        rows = [wholesale];
        return { status: 201, body: wholesale };
      },
    });
    screenOf(owner);
    await screen.findByText(/todavía no tiene listas/);
    press("Crear lista");
    expect(field("Código")).toHaveFocus();
    expect(field("Orden")).toHaveValue("0");
    expect(field("Quién la ve")).toHaveValue("prices.view");
    expect(field("Quién la ve")).toHaveAccessibleDescription(/Sale del tipo/);
    expect(field("IA pública")).toBeDisabled();
    expect(field("IA pública")).toHaveAccessibleDescription(/hasta la Fase 8/);
    fill({ Código: " mayorista ", Nombre: "Mayorista", Tipo: "WHOLESALE" });
    expect(field("Quién la ve")).toHaveValue("prices.view_wholesale");
    fill({ Tipo: "INTERNAL" });
    expect(field("Quién la ve")).toHaveValue("prices.view_internal");
    fill({ Tipo: "WHOLESALE", "Condición de pago": "BANK_TRANSFER", Orden: "x" });
    press("Crear lista", form());
    expect(field("Orden")).toHaveAccessibleDescription(/número entero de 0 a 9999/);
    expect(sent(api, "POST")).toEqual([]);
    fill({ Orden: "20" });
    press("Crear lista", form());
    fireEvent.submit(form()); // otra vez, enseguida
    expect(await screen.findByText("Lista «Mayorista» creada.")).toBeVisible();
    expect(screen.getByText("MAYORISTA")).toBeVisible(); // la lista se volvió a pedir
    await waitFor(() => expect(screen.getByRole("button", { name: "Crear lista" })).toHaveFocus());
    expect(sent(api, "POST")).toEqual([
      {
        ...{ code: "mayorista", name: "Mayorista", sort_order: 20, kind: "WHOLESALE" },
        ...{ payment_condition: "BANK_TRANSFER", is_active: true },
      },
    ]);
  });

  it("crear con el permiso y la predeterminada elegidos los envía; elegido, el tipo ya no lo cambia", async () => {
    const api = mockApi({ [LIST]: page([]), [CREATE]: { status: 201, body: regular } });
    screenOf(owner);
    await screen.findByText(/todavía no tiene listas/);
    press("Crear lista");
    fill({ Código: "VIP", Nombre: "VIP", "Quién la ve": "prices.view_distributor" });
    expect(field("Quién la ve")).not.toHaveAccessibleDescription(/./);
    fill({ Tipo: "INTERNAL" });
    expect(field("Quién la ve")).toHaveValue("prices.view_distributor");
    fireEvent.click(field(/^Predeterminada/));
    fireEvent.click(field(/^Activa/));
    press("Crear lista", form());
    await waitFor(() => expect(sent(api, "POST")).toHaveLength(1));
    expect(sent(api, "POST")[0]).toEqual({
      ...{ code: "VIP", name: "VIP", sort_order: 0, kind: "INTERNAL", payment_condition: null },
      ...{ is_active: false, required_permission: "prices.view_distributor", is_default: true },
    });
  });

  it("edita enviando solo lo cambiado, sin código; cambiar el tipo no cambia quién la ve", async () => {
    let [rows, broken] = [[wholesale], false];
    const api = mockApi({
      [LIST]: () => (broken ? { status: 500, body: {} } : page(rows)),
      "PATCH /api/v1/o/acme/pricing/price-lists/p2/": () => {
        rows = [{ ...wholesale, name: "Mayoristas", kind: "CUSTOM", is_active: true }];
        return { status: 200, body: rows[0] };
      },
    });
    screenOf(owner);
    press("Editar la lista Mayorista", await screen.findByRole("listitem"));
    expect(field("Nombre")).toHaveFocus();
    expect(field("Nombre")).toHaveValue("Mayorista");
    expect(field("Orden")).toHaveValue("20");
    expect(within(form()).queryByLabelText("Código")).toBeNull();
    expect(field("Condición de pago")).toHaveValue("BANK_TRANSFER");
    expect(field(/^Predeterminada/)).not.toBeChecked();
    fill({ Nombre: "Mayoristas", Tipo: "CUSTOM", "Condición de pago": "none" });
    expect(field("Quién la ve")).toHaveValue("prices.view_wholesale");
    expect(field("Quién la ve")).not.toHaveAccessibleDescription(/./);
    fireEvent.click(field(/^Activa/));
    const reply = api.getMockImplementation()!;
    let release = () => {};
    api.mockImplementation(async (input, init) => {
      if (init?.method !== "PATCH") await new Promise<void>((done) => (release = done));
      return reply(input, init);
    });
    press("Guardar", form());
    await waitFor(() => expect(api).toHaveBeenCalledTimes(3)); // la vista, retenida
    expect(within(form()).getByRole("button", { name: "Guardando…" })).toBeVisible();
    api.mockImplementation(reply);
    release();
    expect(await screen.findByText("Lista «Mayoristas» guardada.")).toBeVisible();
    expect(screen.getByText("Activa")).toBeVisible();
    expect(sent(api, "PATCH")).toEqual([
      { name: "Mayoristas", kind: "CUSTOM", payment_condition: null, is_active: true },
    ]);
    press("Editar la lista Mayoristas", screen.getByRole("listitem"));
    press("Guardar", form()); // sin cambios: se cierra sin enviar
    expect(screen.queryByRole("form")).toBeNull();
    expect(sent(api, "PATCH")).toHaveLength(1);
    broken = true; // guardada, pero la vista no vuelve: se dice
    press("Editar la lista Mayoristas", screen.getByRole("listitem"));
    fill({ Nombre: "Mayoristas 2" });
    press("Guardar", form());
    expect(await screen.findByText(/guardada, pero la vista no se pudo actualizar/)).toBeVisible();
  });

  it("los errores van por `code`: el campo, la predeterminada (409 y 400) y sin permiso", async () => {
    let reply: Reply = { status: 409, body: { code: "PRICE_LIST_IS_DEFAULT" } };
    const api = mockApi({ [LIST]: page([regular]), [PATCH]: () => reply });
    screenOf(owner);
    press("Editar la lista Regular", await screen.findByRole("listitem"));
    fireEvent.click(field(/^Activa/));
    const again = async (next: typeof reply, check: () => void) => {
      reply = next;
      press("Guardar", form());
      await waitFor(check);
    };
    await again(reply, () => expect(alert()).toHaveTextContent(/Es la lista predeterminada/));
    await again(bad("is_default"), () =>
      expect(alert()).toHaveTextContent("Solo puede ser la predeterminada una lista activa"),
    );
    await again(bad("name"), () =>
      expect(field("Nombre")).toHaveAccessibleDescription(/El nombre no es válido/),
    );
    expect(field("Nombre")).toHaveFocus();
    await again(bad("sort_order"), () =>
      expect(field("Orden")).toHaveAccessibleDescription(/número entero/),
    );
    await again({ status: 403, body: { code: "PERMISSION_DENIED" } }, () =>
      expect(alert()).toHaveTextContent("No tienes permiso para administrar listas de precios."),
    );
    expect(sent(api, "PATCH")).toEqual(Array(5).fill({ is_active: false }));
  });

  it("al crear, el código repetido o inválido va junto al código", async () => {
    let reply: Reply = { status: 409, body: { code: "PRICE_LIST_CODE_TAKEN" } };
    mockApi({ [LIST]: page([]), [CREATE]: () => reply });
    screenOf(owner);
    await screen.findByText(/todavía no tiene listas/);
    press("Crear lista");
    fill({ Código: "REGULAR", Nombre: "Regular" });
    press("Crear lista", form());
    await waitFor(() =>
      expect(field("Código")).toHaveAccessibleDescription(/Ya hay una lista con ese código/),
    );
    reply = bad("code");
    press("Crear lista", form());
    await waitFor(() =>
      expect(field("Código")).toHaveAccessibleDescription(/El código no es válido/),
    );
  });

  it("si la lista ya no se ve al editar (404), lo dice la vista y la vuelve a pedir", async () => {
    let rows = [regular];
    mockApi({
      [LIST]: () => page(rows),
      [PATCH]: () => {
        rows = [];
        return { status: 404, body: { code: "NOT_FOUND" } };
      },
    });
    screenOf(owner);
    press("Editar la lista Regular", await screen.findByRole("listitem"));
    fill({ Nombre: "Regular 2" });
    press("Guardar", form());
    expect(await screen.findByText(/La lista «Regular» ya no existe/)).toBeVisible();
    expect(await screen.findByText(/todavía no tiene listas/)).toBeVisible();
    expect(screen.getByRole("heading", { name: "Listas de precios" })).toHaveFocus();
  });
});
