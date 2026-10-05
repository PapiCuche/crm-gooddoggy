import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Role, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { RolesList } from "./roles-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/roles/";
const CREATE = "POST /api/v1/o/acme/roles/";
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const role = (id: string, name: string): Role => ({
  id,
  code: `c-${id}`,
  name,
  description: "",
  is_system: false,
  permissions: [],
  members: 0,
  editable: true,
});
const list = (...rows: Role[]) => ({ status: 200, body: { results: rows, next: null } });
const ui = (context = tenant("roles.view", "roles.manage")) => (
  <TenantProvider value={context}>
    <RolesList />
  </TenantProvider>
);
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const trigger = () => screen.getByRole("button", { name: "Crear rol" });
const field = (form: HTMLElement, label: string) => within(form).getByLabelText(label);
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
async function opened() {
  await screen.findByRole("list", { name: "Roles" });
  fireEvent.click(trigger());
  return screen.getByRole("form", { name: "Nuevo rol" });
}
function fill(form: HTMLElement, name: string, description = "") {
  fireEvent.input(field(form, "Nombre"), { target: { value: name } });
  fireEvent.input(field(form, "Descripción (opcional)"), { target: { value: description } });
}
const send = (form: HTMLElement) =>
  fireEvent.click(within(form).getByRole("button", { name: "Crear" }));

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("RoleCreate", () => {
  it("solo se ofrece con el permiso, y abrir o cancelar no envía nada", async () => {
    const api = mockApi({ [LIST]: list(role("r1", "Caja")) });
    const view = renderApp(ui(tenant("roles.view")));
    await screen.findByRole("list", { name: "Roles" });
    expect(screen.queryByRole("button", { name: "Crear rol" })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    const form = await opened();
    expect(screen.queryByRole("button", { name: "Crear rol" })).not.toBeInTheDocument();
    expect(field(form, "Nombre")).toHaveFocus();
    expect(field(form, "Nombre")).toHaveAttribute("maxlength", "100");
    expect(field(form, "Nombre")).toHaveAccessibleDescription(/Hasta 100 caracteres/);
    expect(field(form, "Descripción (opcional)")).toHaveAttribute("maxlength", "255");
    fill(form, "A medias");
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    expect(calls(api, "POST")).toHaveLength(0);
    fireEvent.click(trigger()); // al reabrir, vacío: lo cancelado no se arrastra
    expect(field(screen.getByRole("form"), "Nombre")).toHaveValue("");
  });

  it("crea el rol con un solo envío, lo anuncia y vuelve a pedir la lista", async () => {
    let rows = [role("r1", "Caja")];
    const created = { ...role("r2", "Ventas Norte"), description: "Zona norte" };
    const api = mockApi({
      [LIST]: () => list(...rows),
      [CREATE]: () => ({ status: 201, body: created }),
    });
    renderApp(ui());
    const form = await opened();
    fill(form, "  Ventas   Norte ", " Zona norte ");
    const release = hold(api);
    send(form);
    fireEvent.submit(form); // Enter y un segundo clic mientras se envía: un solo rol
    send(form);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" })); // ni se cierra
    await tick();
    const busy = within(form).getByRole("button", { name: "Creando…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    expect(form).toHaveAttribute("aria-busy", "true");
    rows = [...rows, created];
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "POST")).toHaveLength(1);
    // Sin espacios exteriores; lo demás lo decide la API, que responde con el nombre guardado.
    expect(body(calls(api, "POST")[0])).toEqual({
      name: "Ventas   Norte",
      description: "Zona norte",
    });
    expect(screen.getByText(/Rol «Ventas Norte» creado/)).toHaveAttribute("role", "status");
    await waitFor(() => expect(trigger()).toHaveFocus());
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2)); // la lista, otra vez
    expect(await screen.findByText("Ventas Norte", { selector: ".font-medium" })).toBeVisible();
    fireEvent.click(trigger()); // otro rol: el anuncio anterior se retira y el formulario, vacío
    expect(screen.queryByText(/creado/)).not.toBeInTheDocument();
    expect(field(screen.getByRole("form"), "Nombre")).toHaveValue("");
  });

  it("sin nombre no envía nada: lo dice junto al campo y lleva el foco a él", async () => {
    const api = mockApi({ [LIST]: list(), [CREATE]: { status: 201, body: role("r2", "X") } });
    renderApp(ui());
    const form = await opened();
    fill(form, "   ", "Solo descripción");
    (within(form).getByRole("button", { name: "Crear" }) as HTMLElement).focus();
    send(form);
    const name = field(form, "Nombre");
    expect(within(form).getByRole("alert")).toHaveTextContent("Escribe un nombre para el rol.");
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(name).toHaveFocus();
    expect(calls(api, "POST")).toHaveLength(0);
    fireEvent.input(name, { target: { value: "X" } }); // al corregir, el error se retira
    expect(within(form).queryByRole("alert")).not.toBeInTheDocument();
    expect(field(form, "Descripción (opcional)")).toHaveValue("Solo descripción");
  });

  it.each([
    [409, { code: "ROLE_NAME_TAKEN" }, "Nombre", "Ya hay un rol con ese nombre. Elige otro."],
    [
      400,
      { code: "VALIDATION_ERROR", fields: { name: [{ code: "invalid" }] } },
      "Nombre",
      "Ese nombre no sirve",
    ],
    [
      400,
      { code: "VALIDATION_ERROR", fields: { description: [{ code: "invalid" }] } },
      "Descripción (opcional)",
      "Esa descripción no sirve",
    ],
  ])(
    "un %s de un campo se explica junto a él y no pierde lo escrito",
    async (status, answer, label, text) => {
      const api = mockApi({ [LIST]: list(), [CREATE]: { status, body: answer } });
      renderApp(ui());
      const form = await opened();
      fill(form, "Owner", "Como el de plantilla");
      const submit = within(form).getByRole("button", { name: "Crear" });
      submit.focus();
      send(form);
      const alert = await within(form).findByRole("alert");
      expect(alert).toHaveTextContent(text);
      expect(field(form, label)).toHaveAttribute("aria-invalid", "true");
      expect(field(form, label)).toHaveAccessibleDescription(new RegExp(text));
      // El error del nombre lleva el foco al nombre; el de la descripción lo deja donde estaba.
      await waitFor(() => expect(label === "Nombre" ? field(form, label) : submit).toHaveFocus());
      expect(field(form, "Nombre")).toHaveValue("Owner");
      expect(field(form, "Descripción (opcional)")).toHaveValue("Como el de plantilla");
      expect(screen.queryByText(/creado/)).not.toBeInTheDocument();
      expect(calls(api, "GET")).toHaveLength(1); // nada cambió: la lista no se vuelve a pedir
      fireEvent.input(field(form, "Nombre"), { target: { value: "Otro" } });
      await waitFor(() => expect(within(form).queryByRole("alert")).not.toBeInTheDocument());
      send(form); // el mismo botón reintenta: la marca de envío se soltó
      await within(form).findByRole("alert");
      expect(calls(api, "POST")).toHaveLength(2);
    },
  );

  it.each([
    [403, { code: "PERMISSION_DENIED" }, "No tienes permiso para crear roles."],
    [409, { code: "LAST_OWNER" }, "Debe quedar al menos un Owner activo"],
    [400, { code: "VALIDATION_ERROR", fields: { otro: [] } }, "Algo salió mal de nuestro lado"],
    [400, { code: "VALIDATION_ERROR" }, "Algo salió mal de nuestro lado"],
    [500, { code: "INTERNAL_ERROR" }, "Algo salió mal de nuestro lado"],
    [418, { code: "CODIGO_NUEVO", message: "texto de la API" }, "Algo salió mal de nuestro lado"],
  ])(
    "un %s que no es de un campo se explica en el formulario, por su código",
    async (status, answer, text) => {
      const api = mockApi({ [LIST]: list(), [CREATE]: { status, body: answer } });
      renderApp(ui());
      const form = await opened();
      fill(form, "Caja");
      send(form);
      const alert = await within(form).findByRole("alert");
      expect(alert).toHaveTextContent(text);
      expect(form).not.toHaveTextContent("texto de la API");
      expect(field(form, "Nombre")).not.toHaveAttribute("aria-invalid");
      expect(field(form, "Nombre")).toHaveValue("Caja");
      send(form);
      await waitFor(() => expect(calls(api, "POST")).toHaveLength(2));
    },
  );

  it("sin red el intento falla y se dice: no queda en cola", async () => {
    const api = mockApi({ [LIST]: list(), [CREATE]: { status: 201, body: role("r2", "Caja") } });
    renderApp(ui());
    const form = await opened();
    fill(form, "Caja");
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    send(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(calls(api, "POST")).toHaveLength(1);
    onlineManager.setOnline(true);
    send(form); // con red, el mismo botón lo consigue
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "POST")).toHaveLength(2);
  });

  it("sin sesión va al login una vez y el formulario sigue ocupado, sin error", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/roles", search: "", assign });
    const api = mockApi({
      [LIST]: list(),
      [CREATE]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const form = await opened();
    fill(form, "Caja");
    send(form);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Froles"));
    await tick();
    fireEvent.submit(form); // sigue ocupado hasta que cambia la página
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(calls(api, "POST")).toHaveLength(1);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Creando…" })).toBeInTheDocument();
  });
});
