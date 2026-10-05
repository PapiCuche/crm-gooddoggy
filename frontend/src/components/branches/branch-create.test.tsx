import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Branch, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import messages from "../../../messages/es-PE.json";
import { BranchesList } from "./branches-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/branches/";
const CREATE = "POST /api/v1/o/acme/branches/";
const LABELS = {
  code: "Código",
  name: "Nombre",
  address: "Dirección (opcional)",
  district: "Distrito (opcional)",
  city: "Ciudad (opcional)",
  phone: "Teléfono (opcional)",
};
type Field = keyof typeof LABELS;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const branch = (code: string, name: string): Branch => ({
  id: code,
  code,
  name,
  address: "",
  district: "",
  city: "",
  phone: "",
  timezone: "America/Lima",
  is_active: true,
});
const list = (...rows: Branch[]) => ({ status: 200, body: { results: rows, next: null } });
const ui = (context = tenant("organization.view", "branches.manage")) => (
  <TenantProvider value={context}>
    <BranchesList />
  </TenantProvider>
);
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const trigger = () => screen.getByRole("button", { name: "Crear sucursal" });
const field = (form: HTMLElement, name: Field) => within(form).getByLabelText(LABELS[name]);
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
  await screen.findByText(/en la lista$/); // la lista ya respondió, con filas o sin ellas
  fireEvent.click(await screen.findByRole("button", { name: "Crear sucursal" }));
  return screen.getByRole("form", { name: "Nueva sucursal" });
}
function fill(form: HTMLElement, values: Partial<Record<Field, string>>) {
  for (const [name, value] of Object.entries(values)) {
    fireEvent.input(field(form, name as Field), { target: { value } });
  }
}
// Como una pulsación real: el botón recibe el foco antes del clic.
function send(form: HTMLElement) {
  const button = within(form).getByRole("button", { name: "Crear" });
  button.focus();
  fireEvent.click(button);
}

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("BranchCreate", () => {
  it("solo se ofrece con el permiso, y abrir o cancelar no envía nada", async () => {
    const api = mockApi({ [LIST]: list(branch("LIM", "Lima")) });
    const view = renderApp(ui(tenant("organization.view")));
    await screen.findByRole("list", { name: "Sucursales" });
    expect(screen.queryByRole("button", { name: "Crear sucursal" })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    const form = await opened();
    expect(screen.queryByRole("button", { name: "Crear sucursal" })).not.toBeInTheDocument();
    expect(field(form, "code")).toHaveFocus();
    expect(field(form, "code")).toHaveAccessibleDescription(/Se guarda en mayúsculas/);
    const limits = Object.keys(LABELS).map((name) =>
      field(form, name as Field).getAttribute("maxlength"),
    );
    expect(limits).toEqual(["20", "100", "255", "100", "100", "32"]); // los de la API
    expect(field(form, "phone")).toHaveAttribute("inputmode", "tel");
    expect(within(form).getByText("La zona horaria será America/Lima.")).toBeVisible();
    fill(form, { code: "A-MEDIAS", name: "A medias" });
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    await tick(); // una petición sale unas microtareas después de la pulsación
    expect(calls(api, "POST")).toHaveLength(0);
    fireEvent.click(trigger()); // al reabrir, vacío: lo cancelado no se arrastra
    expect(field(screen.getByRole("form"), "code")).toHaveValue("");
  });

  it("crea la sucursal con un solo envío, lo anuncia y vuelve a pedir la lista", async () => {
    let rows: Branch[] = [];
    const created = { ...branch("LIM-01", "Centro de Lima"), city: "Lima" };
    const api = mockApi({
      [LIST]: () => list(...rows),
      [CREATE]: () => ({ status: 201, body: created }),
    });
    renderApp(ui());
    expect(await screen.findByText("Esta organización todavía no tiene sucursales.")).toBeVisible();
    const form = await opened(); // también se ofrece sin ninguna sucursal
    fill(form, { code: " lim-01 ", name: "  Centro   de Lima ", city: " Lima ", phone: "  " });
    fill(form, { address: " Av. Wilson 1234 ", district: " Cercado " });
    const release = hold(api);
    send(form);
    fireEvent.submit(form); // Enter y un segundo clic mientras se envía: una sola sucursal
    send(form);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" })); // ni se cierra
    await tick();
    const busy = within(form).getByRole("button", { name: "Creando…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    expect(form).toHaveAttribute("aria-busy", "true");
    rows = [created];
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "POST")).toHaveLength(1);
    // Sin espacios exteriores; lo demás lo decide la API, que responde con lo que guardó.
    expect(body(calls(api, "POST")[0])).toEqual({
      code: "lim-01",
      name: "Centro   de Lima",
      address: "Av. Wilson 1234",
      district: "Cercado",
      city: "Lima",
      phone: "",
    });
    const done = screen.getByText("Sucursal «Centro de Lima» creada. Va al final de la lista.");
    expect(done).toHaveAttribute("role", "status");
    expect(done.textContent).toContain("«Centro de Lima»"); // exacto: `getByText` junta espacios
    expect(done).not.toHaveClass("sr-only"); // se ve
    await waitFor(() => expect(trigger()).toHaveFocus());
    expect(await screen.findByText("Centro de Lima", { selector: ".font-medium" })).toBeVisible();
    expect(calls(api, "GET")).toHaveLength(2); // la lista, otra vez
    expect(screen.queryByText(/todavía no tiene sucursales/)).not.toBeInTheDocument();
    fireEvent.click(trigger()); // otra: el anuncio anterior se retira y el formulario, vacío
    expect(done).toHaveTextContent("");
    expect(field(screen.getByRole("form"), "name")).toHaveValue("");
  });

  it("sin código o sin nombre no envía nada: lo dice junto al campo y lleva el foco al primero", async () => {
    const api = mockApi({ [LIST]: list() });
    renderApp(ui());
    const form = await opened();
    fill(form, { code: "   ", city: "Lima" });
    send(form);
    expect(field(form, "code")).toHaveAccessibleDescription("Escribe un código para la sucursal.");
    expect(field(form, "name")).toHaveAccessibleDescription("Escribe un nombre para la sucursal.");
    expect(field(form, "code")).toHaveFocus();
    send(form); // otra vez, desde el botón: el foco vuelve al campo
    expect(field(form, "code")).toHaveFocus();
    fill(form, { code: "LIM" }); // al corregir, los avisos se retiran hasta el siguiente envío
    expect(field(form, "name")).not.toHaveAttribute("aria-invalid");
    send(form);
    expect(field(form, "code")).not.toHaveAttribute("aria-invalid");
    expect(field(form, "name")).toHaveFocus();
    expect(field(form, "city")).toHaveValue("Lima"); // lo escrito no se pierde
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    fireEvent.click(trigger()); // al reabrir no queda el aviso anterior
    expect(within(screen.getByRole("form")).queryByRole("alert")).not.toBeInTheDocument();
    await tick();
    expect(calls(api, "POST")).toHaveLength(0);
  });

  it("un campo rellenado sin evento no deja el aviso de «falta» tapando la respuesta", async () => {
    const api = mockApi({ [LIST]: list(), [CREATE]: { status: 500, body: { code: "X" } } });
    renderApp(ui());
    const form = await opened();
    send(form); // faltan los dos
    for (const name of ["code", "name"] as const)
      fireEvent.change(field(form, name), { target: { value: "LIM" } }); // sin `input`
    send(form);
    fireEvent.submit(form); // retirar el aviso no suelta la marca
    expect(await within(form).findByText(/Algo salió mal/)).toHaveAttribute("role", "alert");
    expect(within(form).getAllByRole("alert")).toHaveLength(1);
    expect(calls(api, "POST")).toHaveLength(1);
  });

  it("un código repetido se explica junto al código, con el foco en él, y se puede corregir", async () => {
    let reply: { status: number; body: unknown } = {
      status: 409,
      body: { code: "BRANCH_CODE_TAKEN", message: "<b>texto de la API</b>" },
    };
    const api = mockApi({ [LIST]: list(), [CREATE]: () => reply });
    renderApp(ui());
    const form = await opened();
    fill(form, { code: "LIM", name: "Lima" });
    send(form);
    await waitFor(() =>
      expect(field(form, "code")).toHaveAccessibleDescription(
        "Ya hay una sucursal con ese código. Elige otro.",
      ),
    );
    expect(field(form, "code")).toHaveFocus();
    send(form); // sin corregir: el mismo error, otra vez, vuelve a llevar el foco
    await waitFor(() => expect(field(form, "code")).toHaveFocus());
    expect(form).not.toHaveTextContent("texto de la API"); // nunca el texto de la respuesta
    expect(field(form, "name")).toHaveValue("Lima");
    fill(form, { code: "LIM-2" });
    await waitFor(() => expect(field(form, "code")).not.toHaveAttribute("aria-invalid"));
    reply = { status: 201, body: branch("LIM-2", "Lima") };
    send(form);
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "POST")).toHaveLength(3);
  });

  it("cada campo que la API no acepta lo dice junto a él, y el foco va al primero", async () => {
    const fields = Object.fromEntries(
      ["phone", "city", "name", "constructor"].map((name) => [name, [{ code: "invalid" }]]),
    );
    mockApi({
      [LIST]: list(),
      [CREATE]: { status: 400, body: { code: "VALIDATION_ERROR", fields } },
    });
    renderApp(ui());
    const form = await opened();
    fill(form, { code: "LIM", name: "ㅤ" });
    (document.activeElement as HTMLElement).blur(); // el foco, en ninguna parte
    fireEvent.submit(form);
    await waitFor(() => expect(field(form, "name")).toHaveAttribute("aria-invalid", "true"));
    const bad = Object.keys(LABELS).filter((name) =>
      field(form, name as Field).hasAttribute("aria-invalid"),
    );
    expect(bad).toEqual(["name", "city", "phone"]);
    expect(field(form, "name")).toHaveAccessibleDescription(/Ese nombre no sirve/);
    expect(field(form, "phone")).toHaveAccessibleDescription(/Ese teléfono no sirve/);
    expect(field(form, "name")).toHaveFocus(); // el primero en el orden del formulario
    expect(within(form).getAllByRole("alert")).toHaveLength(3); // sin un error general además
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    fireEvent.click(trigger()); // al reabrir no queda el error anterior
    expect(within(screen.getByRole("form")).queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([
    [403, { code: "PERMISSION_DENIED" }, "No tienes permiso para crear sucursales."],
    [400, { code: "VALIDATION_ERROR", fields: { timezone: [] } }, "Algo salió mal de nuestro lado"],
    [400, { code: "VALIDATION_ERROR" }, "Algo salió mal de nuestro lado"],
    [500, { code: "INTERNAL_ERROR", fields: { name: [] } }, "Algo salió mal de nuestro lado"],
    [409, { code: "CODIGO_NUEVO", message: "texto de la API" }, "Algo salió mal de nuestro lado"],
  ])("un %i se explica en el formulario, sin perder lo escrito", async (status, reply, text) => {
    const api = mockApi({ [LIST]: list(), [CREATE]: { status, body: reply } });
    renderApp(ui());
    const form = await opened();
    fill(form, { code: "LIM", name: "Lima", address: "Av. Wilson 1234" });
    send(form);
    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent(text);
    expect(form).not.toHaveTextContent("texto de la API");
    expect(field(form, "address")).toHaveValue("Av. Wilson 1234");
    expect(within(form).getByRole("button", { name: "Crear" })).toHaveFocus(); // no se mueve
    fill(form, { address: "Av. Wilson 1235" }); // al escribir, el error se retira
    await waitFor(() => expect(within(form).queryByRole("alert")).not.toBeInTheDocument());
    send(form); // y el mismo botón reintenta
    await within(form).findByRole("alert");
    expect(calls(api, "POST")).toHaveLength(2);
  });

  it("sin red el intento falla y se dice: no queda en cola", async () => {
    const api = mockApi({ [LIST]: list(), [CREATE]: { status: 201, body: branch("LIM", "Lima") } });
    renderApp(ui());
    const form = await opened();
    fill(form, { code: "LIM", name: "Lima" });
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
    vi.stubGlobal("location", { origin, pathname: "/o/acme/sucursales", search: "", assign });
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
    fill(form, { code: "LIM", name: "Lima" });
    send(form);
    await waitFor(() =>
      expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Fsucursales"),
    );
    await tick();
    fill(form, { name: "Lima Centro" }); // escribir no desocupa el formulario
    fireEvent.submit(form);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(form).toHaveAttribute("aria-busy", "true");
    expect(within(form).queryByRole("alert")).not.toBeInTheDocument();
    expect(calls(api, "POST")).toHaveLength(1);
    expect(assign).toHaveBeenCalledTimes(1);
  });

  it("reintentar y escribir antes de que la pantalla diga «Creando…» no suelta la marca", async () => {
    const api = mockApi({ [LIST]: list(), [CREATE]: { status: 500, body: { code: "X" } } });
    renderApp(ui());
    const form = await opened();
    fill(form, { code: "LIM", name: "Lima" });
    send(form);
    await within(form).findByRole("alert");
    const release = hold(api);
    send(form); // reintento: la pantalla aún enseña el error anterior
    fill(form, { name: "Lima 2" }); // y se escribe en esa misma tarea
    send(form);
    fireEvent.submit(form);
    await tick();
    expect(calls(api, "POST")).toHaveLength(2); // el primero y un solo reintento
    release();
    await within(form).findByRole("alert");
  });

  it("un render ajeno justo antes de pulsar no deja la marca a merced de un efecto pasivo", async () => {
    const api = mockApi({ [LIST]: list(), [CREATE]: { status: 201, body: branch("LIM", "Lima") } });
    let again = () => {};
    // Cada render da un contexto nuevo: `BranchesList` y el formulario se vuelven a pintar.
    function Shell() {
      const [turn, setTurn] = useState(0);
      again = () => setTurn(turn + 1);
      return <div data-turn={turn}>{ui(tenant("organization.view", "branches.manage"))}</div>;
    }
    renderApp(<Shell />);
    const form = await opened();
    fill(form, { code: "LIM", name: "Lima" });
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

  it("el foco no se mueve solo cuando el usuario ya está en otra parte", async () => {
    let reply: { status: number; body: unknown } = {
      status: 409,
      body: { code: "BRANCH_CODE_TAKEN" },
    };
    const api = mockApi({ [LIST]: list(), [CREATE]: () => reply });
    renderApp(ui());
    const form = await opened();
    fill(form, { code: "LIM", name: "Lima" });
    let release = hold(api);
    send(form);
    await tick();
    field(form, "city").focus(); // mientras se envía, el usuario sigue rellenando
    release();
    await waitFor(() => expect(field(form, "code")).toHaveAttribute("aria-invalid", "true"));
    expect(field(form, "city")).toHaveFocus();
    reply = { status: 201, body: branch("LIM", "Lima") };
    release = hold(api);
    send(form);
    await tick();
    const heading = screen.getByRole("heading", { level: 1 });
    heading.focus(); // se fue al título: al cerrarse el formulario, el foco tampoco se le quita
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(heading).toHaveFocus();
  });

  it("Enter mantenido no vuelve a pulsar: ni reabre el formulario ni reenvía", async () => {
    const api = mockApi({ [LIST]: list(), [CREATE]: { status: 500, body: { code: "X" } } });
    renderApp(ui());
    await screen.findByText(/en la lista$/);
    expect(document.body).toHaveFocus(); // al montar, «Crear sucursal» no toma el foco
    expect(fireEvent.keyDown(trigger(), { key: "Enter", repeat: true })).toBe(false);
    const form = await opened();
    fill(form, { code: "LIM", name: "Lima" });
    send(form);
    await within(form).findByRole("alert");
    const held = fireEvent.keyDown(within(form).getByRole("button", { name: "Crear" }), {
      key: "Enter",
      repeat: true,
    });
    expect(held).toBe(false); // la pulsación repetida se descarta
    const once = fireEvent.keyDown(field(form, "name"), { key: "Enter" });
    expect(once).toBe(true); // una pulsación normal, no
    expect(fireEvent.keyDown(field(form, "name"), { key: "a", repeat: true })).toBe(true);
    expect(calls(api, "POST")).toHaveLength(1);
  });
});
