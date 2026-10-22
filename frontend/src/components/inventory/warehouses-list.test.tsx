import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Warehouse } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { WarehousesList } from "./warehouses-list";

const LIST = "GET /api/v1/o/acme/warehouses/";
const CREATE = "POST /api/v1/o/acme/warehouses/";
const PATCH = "PATCH /api/v1/o/acme/warehouses/w1/";
const BRANCHES = "GET /api/v1/o/acme/branches/?limit=200";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const viewer: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("inventory.view"),
};
const manager = {
  ...viewer,
  permissions: grants("inventory.view", "warehouses.manage", "organization.view"),
};
const central: Warehouse = {
  ...{ id: "w1", branch_id: "b1", code: "CEN", name: "Central", type: "WAREHOUSE" },
  ...{ is_sellable: true, is_active: true },
};
const transit: Warehouse = {
  ...{ ...central, id: "w2", code: "TR-1", name: "Camión", type: "TRANSIT" },
  ...{ is_sellable: false, is_active: false },
};
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const branch = (id: string, name: string, code: string, is_active = true) => ({
  ...{ id, name, code, is_active, address: "", district: "", city: "", phone: "" },
  timezone: "America/Lima",
});
const BRANCH_LIST = page([branch("b1", "Miraflores", "MIR"), branch("b2", "Surco", "SUR", false)]);

const screenOf = (tenant: SelfContext) =>
  renderApp(
    <TenantProvider value={tenant}>
      <WarehousesList />
    </TenantProvider>,
  );
const sent = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls
    .filter(([, init]) => init?.method === method)
    .map(([, init]) => JSON.parse(String(init?.body)));
const form = () => screen.getByRole("form");
const field = (label: string | RegExp) => within(form()).getByLabelText(label);
const alert = () => within(form()).getByRole("alert");
// Como una pulsación real: el botón recibe el foco antes del clic.
function press(name: string, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus();
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

describe("WarehousesList", () => {
  it("enseña cada almacén con sucursal, estado, tipo y venta; sin `warehouses.manage`, sin acciones", async () => {
    const api = mockApi({ [LIST]: page([central, transit]), [BRANCHES]: BRANCH_LIST });
    screenOf({ ...viewer, permissions: grants("inventory.view", "organization.view") });
    const list = await screen.findByRole("list", { name: "Almacenes" });
    const [first, second] = within(list).getAllByRole("listitem");
    expect(within(first!).getByText("CEN")).toHaveClass("font-mono");
    expect(within(first!).getByText("Activo")).toHaveClass("text-success");
    expect(await within(first!).findByText("Sucursal: Miraflores (MIR)")).toBeVisible();
    expect(first).toHaveTextContent("Cuenta para la venta");
    expect(within(second!).getByText("Inactivo")).toHaveClass("text-muted");
    for (const text of ["En tránsito", "No cuenta para la venta"]) {
      expect(within(second!).getByText(text)).toBeVisible();
    }
    expect(screen.getByText("2 almacenes en la lista")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    expect(api).toHaveBeenCalledTimes(2);
  });

  it("sin `organization.view` no pide sucursales ni deja crear; vacío y sin permiso se explican", async () => {
    const api = mockApi({ [LIST]: page([central]) });
    screenOf({ ...manager, permissions: grants("inventory.view", "warehouses.manage") });
    await screen.findByRole("list", { name: "Almacenes" });
    expect(screen.queryByText(/Sucursal:/)).toBeNull();
    press("Crear almacén");
    expect(field("Sucursal")).toHaveAccessibleDescription(/No se pudo cargar la lista/);
    fill({ Código: "CEN", Nombre: "Central" });
    press("Crear almacén", form());
    expect(field("Sucursal")).toHaveAccessibleDescription("Elige la sucursal.");
    expect(api).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
    mockApi({ [LIST]: page([]) });
    const empty = screenOf(viewer);
    expect(await screen.findByText(/todavía no tiene almacenes/)).toBeVisible();
    empty.unmount();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf(viewer);
    expect(await screen.findByText(/No tienes permiso para ver el inventario/)).toBeVisible();
  });

  it("crea con sucursal, tipo y venta; sin sucursal no envía; un envío por pulsación", async () => {
    let rows: Warehouse[] = [];
    const api = mockApi({
      [BRANCHES]: BRANCH_LIST,
      [LIST]: () => page(rows),
      [CREATE]: () => {
        rows = [transit];
        return { status: 201, body: transit };
      },
    });
    screenOf(manager);
    await screen.findByText(/todavía no tiene almacenes/);
    press("Crear almacén");
    expect(field("Código")).toHaveFocus();
    await waitFor(() => expect(field("Sucursal")).toHaveLength(3));
    const options = [...(field("Sucursal") as HTMLSelectElement).options].map((o) => o.text);
    expect(options).toEqual(["Elige una sucursal", "Miraflores (MIR)", "Surco (SUR), inactiva"]);
    expect(within(form()).queryByLabelText(/^Activo/)).toBeNull(); // nace activo
    fill({ Código: " tr-1 ", Nombre: "Camión", Tipo: "TRANSIT" });
    press("Crear almacén", form());
    expect(field("Sucursal")).toHaveAccessibleDescription("Elige la sucursal.");
    expect(sent(api, "POST")).toEqual([]);
    fill({ Sucursal: "b2" });
    expect(field("Sucursal")).not.toHaveAccessibleDescription(/./);
    fireEvent.click(field("Su stock cuenta para la venta"));
    const reply = api.getMockImplementation()!;
    let release = () => {};
    api.mockImplementationOnce(async (input, init) => {
      await new Promise<void>((done) => (release = done)); // el alta tarda: se suelta a mano
      return reply(input, init);
    });
    press("Crear almacén", form());
    fireEvent.submit(form()); // otra vez, enseguida
    fireEvent.click(field("Su stock cuenta para la venta")); // mientras se crea, no cambia
    expect(field("Su stock cuenta para la venta")).not.toBeChecked();
    await waitFor(() => expect(sent(api, "POST")).toHaveLength(1));
    release();
    expect(await screen.findByText("Almacén «Camión» creado.")).toBeVisible();
    expect(screen.getByText("TR-1")).toBeVisible(); // la lista se volvió a pedir
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Crear almacén" })).toHaveFocus(),
    );
    const body = { code: "tr-1", name: "Camión", type: "TRANSIT", is_sellable: false };
    expect(sent(api, "POST")).toEqual([{ ...body, is_active: true, branch_id: "b2" }]);
  });

  it("los errores del alta se dicen por `code`, junto a su campo o en el formulario", async () => {
    let reply = { status: 409, body: { code: "WAREHOUSE_CODE_TAKEN" } as object };
    let gone = false; // tras el 400, Miraflores ya no está
    const api = mockApi({
      [BRANCHES]: () => (gone ? page([branch("b2", "Surco", "SUR")]) : BRANCH_LIST),
      [LIST]: page([]),
      [CREATE]: () => reply,
    });
    screenOf(manager);
    await screen.findByText(/todavía no tiene almacenes/);
    press("Crear almacén");
    await waitFor(() => expect(field("Sucursal")).toHaveLength(3));
    fill({ Código: "CEN", Nombre: "Central", Sucursal: "b1" });
    const again = async (next: typeof reply, check: () => void) => {
      reply = next;
      press("Crear almacén", form());
      await waitFor(check);
    };
    await again(reply, () =>
      expect(field("Código")).toHaveAccessibleDescription(/Ya hay un almacén con ese código/),
    );
    expect(field("Código")).toHaveFocus();
    const bad = (name: string) => ({
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { [name]: ["x"] } },
    });
    await again(bad("code"), () =>
      expect(field("Código")).toHaveAccessibleDescription(/El código no es válido/),
    );
    gone = true;
    await again(bad("branch_id"), () =>
      expect(alert()).toHaveTextContent("La sucursal elegida ya no existe"),
    );
    const reads = () => api.mock.calls.filter(([url]) => String(url).includes("branches"));
    await waitFor(() => expect(reads()).toHaveLength(2)); // las sucursales, otra vez
    await waitFor(() => expect(field("Sucursal")).toHaveValue(""));
    press("Crear almacén", form()); // lo que ya no está en la lista no se envía
    expect(field("Sucursal")).toHaveAccessibleDescription("Elige la sucursal.");
    expect(sent(api, "POST")).toHaveLength(3);
    fill({ Sucursal: "b2" });
    await again({ status: 403, body: { code: "PERMISSION_DENIED" } }, () =>
      expect(alert()).toHaveTextContent("No tienes permiso para administrar almacenes."),
    );
    const held = within(form()).getByRole("button", { name: "Crear almacén" });
    expect(fireEvent.keyDown(held, { key: "Enter", repeat: true })).toBe(false);
  });

  it("edita nombre, tipo, venta y estado; envía solo lo cambiado, nunca código ni sucursal", async () => {
    let rows = [central];
    let [gone, broken] = [false, false];
    const api = mockApi({
      [BRANCHES]: BRANCH_LIST,
      [LIST]: () => (broken ? { status: 500, body: {} } : page(rows)),
      [PATCH]: () => {
        rows = gone ? [] : [{ ...central, name: "Central Lima", is_active: false }];
        return gone ? { status: 404, body: { code: "NOT_FOUND" } } : { status: 200, body: rows[0] };
      },
    });
    screenOf(manager);
    press("Editar el almacén Central", await screen.findByRole("listitem"));
    expect(field("Nombre")).toHaveFocus();
    expect(field("Nombre")).toHaveValue("Central");
    expect(within(form()).queryByLabelText("Código")).toBeNull();
    expect(field("Su stock cuenta para la venta")).toBeChecked();
    fill({ Nombre: "Central Lima" });
    fireEvent.click(field(/^Activo/));
    press("Guardar", form());
    expect(await screen.findByText("Almacén «Central Lima» guardado.")).toBeVisible();
    expect(screen.getByText("Inactivo")).toBeVisible();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Editar el almacén Central Lima" })).toHaveFocus(),
    );
    expect(sent(api, "PATCH")).toEqual([{ name: "Central Lima", is_active: false }]);
    broken = true; // guardado, pero la lista no vuelve: se dice
    press("Editar el almacén Central Lima", screen.getByRole("listitem"));
    press("Guardar", form());
    expect(await screen.findByText(/guardado, pero la lista no se pudo actualizar/)).toBeVisible();
    [broken, gone] = [false, true]; // si ya no existe al editar, lo dice la lista y se vuelve a pedir
    press("Editar el almacén Central Lima", screen.getByRole("listitem"));
    press("Guardar", form());
    expect(await screen.findByText(/El almacén «Central Lima» ya no existe/)).toBeVisible();
    expect(await screen.findByText(/todavía no tiene almacenes/)).toBeVisible();
    expect(screen.getByRole("heading", { name: "Almacenes" })).toHaveFocus();
  });

  it("reabrir vuelve a pedir las sucursales que fallaron; editar no deshace lo que otro cambió", async () => {
    let fail = true;
    let rows = [central];
    const api = mockApi({
      [BRANCHES]: () => (fail ? { status: 500, body: {} } : BRANCH_LIST),
      [LIST]: () => page(rows),
      [PATCH]: () => ({ status: 200, body: rows[0] }),
    });
    const view = screenOf(manager);
    await screen.findByRole("listitem");
    press("Crear almacén");
    await waitFor(() => expect(field("Sucursal")).toHaveAccessibleDescription(/No se pudo cargar/));
    fail = false;
    press("Cancelar", form());
    press("Crear almacén");
    await waitFor(() => expect(field("Sucursal")).toHaveLength(3));
    press("Cancelar", form());
    press("Editar el almacén Central", screen.getByRole("listitem"));
    rows = [{ ...central, is_active: false }]; // otra persona lo desactiva mientras se edita
    await view.client.invalidateQueries({ queryKey: ["/api/v1/o/acme/warehouses/"] });
    await waitFor(() => expect(screen.getByText("Inactivo")).toBeVisible());
    fill({ Nombre: "Central Lima" });
    press("Guardar", form());
    await waitFor(() => expect(sent(api, "PATCH")).toEqual([{ name: "Central Lima" }]));
  });
});
