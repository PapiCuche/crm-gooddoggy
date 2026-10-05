import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { useState } from "react";
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
    await tick(); // una petición sale unas microtareas después de la pulsación
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

// Revisión de F2-35: lo que la primera tanda de tests no fijaba.
describe("RoleCreate: marca de envío, foco y avisos", () => {
  const twice = () => {
    let sent = 0;
    return () =>
      ++sent === 1
        ? { status: 500, body: { code: "INTERNAL_ERROR" } }
        : { status: 201, body: role(`r${sent}`, `Caja ${sent}`) };
  };

  it("reintentar y escribir antes de que la pantalla diga «Creando…» no suelta la marca", async () => {
    const api = mockApi({ [LIST]: list(), [CREATE]: twice() });
    renderApp(ui());
    const form = await opened();
    fill(form, "Caja");
    send(form);
    await within(form).findByRole("alert");
    const release = hold(api);
    send(form); // el mismo botón reintenta…
    fireEvent.input(field(form, "Nombre"), { target: { value: "Cajas" } }); // …y se sigue escribiendo
    await tick();
    expect(form).toHaveAttribute("aria-busy", "true");
    fireEvent.submit(form);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(screen.getByRole("form")).toBe(form); // ni se cierra con la escritura en vuelo
    expect(calls(api, "POST")).toHaveLength(2); // el 500 y un solo reintento
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "POST")).toHaveLength(2);
  });

  it("sin sesión, escribir no desocupa el formulario", async () => {
    const api = mockApi({
      [LIST]: list(),
      [CREATE]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    renderApp(ui());
    const form = await opened();
    fill(form, "Caja");
    send(form);
    await tick();
    fill(form, "Cajas", "otra");
    await tick();
    fireEvent.submit(form);
    await tick();
    expect(form).toHaveAttribute("aria-busy", "true");
    expect(calls(api, "POST")).toHaveLength(1);
  });

  it.each([
    ["un error", { status: 500, body: { code: "INTERNAL_ERROR" } }],
    ["el alta", { status: 201, body: role("r2", "Caja") }],
  ])("una pulsación entre la respuesta (%s) y el render no reenvía", async (_, reply) => {
    const api = mockApi({ [LIST]: list(), [CREATE]: reply });
    renderApp(ui());
    const form = await opened();
    fill(form, "Caja");
    const release = hold(api);
    send(form);
    await tick();
    await act(async () => {
      release();
      for (let turn = 0; turn < 100; turn++) await null; // llega la respuesta; aún no hay render
      fireEvent.submit(form);
    });
    await tick();
    expect(calls(api, "POST")).toHaveLength(1);
  });

  it("un render ajeno justo antes de pulsar no deja la marca a merced de un efecto pasivo", async () => {
    const api = mockApi({ [LIST]: list(), [CREATE]: { status: 201, body: role("r2", "Caja") } });
    let again = () => {};
    // Cada render da un contexto nuevo: `RolesList` y el formulario se vuelven a pintar.
    function Shell() {
      const [turn, setTurn] = useState(0);
      again = () => setTurn(turn + 1);
      return <div data-turn={turn}>{ui(tenant("roles.view", "roles.manage"))}</div>;
    }
    renderApp(<Shell />);
    const form = await opened();
    fill(form, "Caja");
    hold(api); // la escritura no llega a responder
    const submit = within(form).getByRole("button", { name: "Crear" });
    const watch = new MutationObserver(() => submit.click()); // pulsa tras ese render
    watch.observe(form.closest("[data-turn]")!, { attributes: true });
    setTimeout(again); // fuera de un evento: sus efectos pasivos llegan después
    await waitFor(() => expect(calls(api, "POST")).toHaveLength(1));
    watch.disconnect();
    await tick();
    fireEvent.click(submit); // otra pulsación con la escritura en vuelo
    await tick();
    expect(calls(api, "POST")).toHaveLength(1);
  });

  it("el foco no se mueve solo, ni cuando el usuario ya está en otra parte", async () => {
    let reply: { status: number; body: unknown } = { status: 201, body: role("r2", "Nuevo") };
    const api = mockApi({ [LIST]: list(role("r1", "Caja")), [CREATE]: () => reply });
    renderApp(ui());
    await screen.findByRole("list", { name: "Roles" });
    expect(document.body).toHaveFocus(); // al montar, «Crear rol» no toma el foco
    let form = await opened();
    fill(form, "Nuevo");
    let release = hold(api);
    send(form);
    await tick();
    const heading = screen.getByRole("heading", { level: 1 });
    heading.focus(); // el usuario se fue al título mientras se enviaba
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    await tick();
    expect(heading).toHaveFocus();

    reply = { status: 409, body: { code: "ROLE_NAME_TAKEN" } };
    form = await opened();
    fill(form, "Nuevo");
    release = hold(api);
    send(form);
    await tick();
    const cancel = within(form).getByRole("button", { name: "Cancelar" });
    cancel.focus(); // ya no está en el botón pulsado
    release();
    await within(form).findByRole("alert");
    await tick();
    expect(cancel).toHaveFocus();
    cancel.blur(); // en ninguna parte: el mismo error, otra vez, lleva el foco al nombre
    expect(document.body).toHaveFocus();
    release = hold(api);
    fireEvent.submit(form);
    await tick();
    release();
    await waitFor(() => expect(field(form, "Nombre")).toHaveFocus());
  });

  it("al reabrir no queda el error anterior, y cada campo retira su error al corregirlo", async () => {
    const api = mockApi({
      [LIST]: list(),
      [CREATE]: {
        status: 400,
        body: { code: "VALIDATION_ERROR", fields: { description: [{ code: "invalid" }] } },
      },
    });
    renderApp(ui());
    let form = await opened();
    fill(form, "Caja", "x");
    send(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("Esa descripción no sirve");
    fireEvent.input(field(form, "Descripción (opcional)"), { target: { value: "y" } });
    await waitFor(() => expect(within(form).queryByRole("alert")).not.toBeInTheDocument());
    send(form);
    await within(form).findByRole("alert");
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    form = await opened();
    await tick();
    expect(within(form).queryByRole("alert")).not.toBeInTheDocument();
    const submit = within(form).getByRole("button", { name: "Crear" });
    send(form); // sin nombre
    expect(within(form).getByRole("alert")).toHaveTextContent("Escribe un nombre");
    submit.focus();
    send(form); // otra vez sin nombre, desde el botón: el foco vuelve al campo
    expect(field(form, "Nombre")).toHaveFocus();
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    form = await opened();
    expect(within(form).queryByRole("alert")).not.toBeInTheDocument();
    expect(calls(api, "POST")).toHaveLength(2);
  });

  it("el aviso existe antes de tener texto, se ve, y dice el nombre que guardó la API", async () => {
    const api = mockApi({
      [LIST]: list(),
      [CREATE]: { status: 201, body: role("r2", "Ventas Norte") },
    });
    renderApp(ui());
    const form = await opened();
    const notice = form.parentElement!.querySelector('[role="status"]')!;
    expect(notice).toBeEmptyDOMElement();
    expect(notice).toHaveClass("sr-only");
    fill(form, "ventas   norte");
    const release = hold(api);
    send(form);
    await tick();
    expect(within(form).getByRole("button", { name: "Cancelar" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(notice).toBeInTheDocument(); // el mismo nodo: una región viva que cambia, no una nueva
    expect(notice.textContent).toBe(
      "Rol «Ventas Norte» creado, sin permisos. Va al final de la lista.",
    );
    expect(notice).not.toHaveClass("sr-only");
    expect(calls(api, "POST")).toHaveLength(1);
  });

  it("Enter mantenido no vuelve a pulsar: ni reabre el formulario ni reenvía", async () => {
    mockApi({ [LIST]: list() });
    renderApp(ui());
    await screen.findByRole("list", { name: "Roles" });
    expect(fireEvent.keyDown(trigger(), { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(trigger(), { key: "Enter" })).toBe(true);
    const form = await opened();
    expect(fireEvent.keyDown(field(form, "Nombre"), { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(field(form, "Nombre"), { key: "a", repeat: true })).toBe(true);
    expect(fireEvent.keyDown(field(form, "Nombre"), { key: "Tab", repeat: true })).toBe(true);
  });
});
