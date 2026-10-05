import { onlineManager } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Branch, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { BranchesList } from "./branches-list";

const router = { replace: vi.fn() };
vi.mock("next/navigation", () => ({ useRouter: () => router }));

const LIST = "GET /api/v1/o/acme/branches/";
const EDIT = (id: string) => `PATCH /api/v1/o/acme/branches/${id}/`;
const LABELS = {
  name: "Nombre",
  address: "Dirección (opcional)",
  district: "Distrito (opcional)",
  city: "Ciudad (opcional)",
  phone: "Teléfono (opcional)",
  timezone: "Zona horaria",
};
type Field = keyof typeof LABELS;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const branch = (id: string, name: string, extra: Partial<Branch> = {}): Branch => ({
  id,
  code: id.toUpperCase(),
  name,
  address: "",
  district: "",
  city: "",
  phone: "",
  timezone: "America/Lima",
  is_active: true,
  ...extra,
});
const lima = branch("b2", "Lima", { address: "Av. Wilson 1234", city: "Lima", phone: "555" });
const branches = [branch("b1", "Arequipa"), lima, branch("b3", "Cusco", { is_active: false })];
const list = (results: Branch[] = branches) => ({ status: 200, body: { results, next: null } });
const ui = (context = tenant("organization.view", "branches.manage")) => (
  <TenantProvider value={context}>
    <BranchesList />
  </TenantProvider>
);
const card = (name: string) =>
  screen.getByText(name, { selector: "span.font-medium" }).closest("li") as HTMLElement;
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const trigger = (name = "Lima") =>
  screen.getByRole("button", { name: `Editar la sucursal ${name}` });
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
async function opened(name = "Lima") {
  await screen.findByRole("list", { name: "Sucursales" });
  fireEvent.click(trigger(name));
  return screen.getByRole("form", { name: `Editar ${name}` });
}
function fill(form: HTMLElement, values: Partial<Record<Field, string>>) {
  for (const [name, value] of Object.entries(values)) {
    fireEvent.input(field(form, name as Field), { target: { value } });
  }
}
// Como una pulsación real: el botón recibe el foco antes del clic.
function send(form: HTMLElement) {
  const button = within(form).getByRole("button", { name: "Guardar" });
  button.focus();
  fireEvent.click(button);
}

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  onlineManager.setOnline(true);
});

describe("BranchEditAction", () => {
  it("se ofrece con el permiso, en cada sucursal; abrir enseña lo que hay y no envía nada", async () => {
    const api = mockApi({ [LIST]: list() });
    const view = renderApp(ui(tenant("organization.view")));
    await screen.findByRole("list", { name: "Sucursales" });
    expect(screen.queryByRole("button", { name: /^Editar/ })).not.toBeInTheDocument();
    view.unmount();
    // Un navegador que lista `UTC` (V8 no lo hace): se ofrece una vez, la primera.
    vi.spyOn(Intl, "supportedValuesOf").mockReturnValue(["America/Bogota", "America/Lima", "UTC"]);
    renderApp(ui());
    await screen.findByRole("list", { name: "Sucursales" });
    expect(screen.getAllByRole("button", { name: /^Editar la sucursal/ })).toHaveLength(3);
    expect(trigger("Cusco")).toBeVisible(); // también una inactiva
    const form = await opened();
    expect(within(card("Lima")).queryByRole("button", { name: /^Editar/ })).toBeNull();
    expect(field(form, "name")).toHaveFocus();
    const shown = Object.keys(LABELS).map((name) => [
      (field(form, name as Field) as HTMLInputElement).value,
      field(form, name as Field).getAttribute("maxlength"),
    ]);
    expect(shown).toEqual([
      ["Lima", "100"],
      ["Av. Wilson 1234", "255"],
      ["", "100"],
      ["Lima", "100"],
      ["555", "32"],
      ["America/Lima", "64"],
    ]);
    expect(within(form).queryByLabelText("Código")).not.toBeInTheDocument(); // no se edita
    const zone = field(form, "timezone");
    expect(zone).toHaveAccessibleDescription(/como America\/Lima/);
    const options = [
      ...form.querySelectorAll(`datalist#${CSS.escape(zone.getAttribute("list")!)} option`),
    ];
    const offered = options.map((option) => option.getAttribute("value"));
    expect(offered).toEqual(["UTC", "America/Bogota", "America/Lima"]);
    fill(form, { name: "A medias" });
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    await tick();
    expect(calls(api, "PATCH")).toHaveLength(0);
    fireEvent.click(trigger()); // al reabrir, lo que hay: lo cancelado no se arrastra
    expect(field(screen.getByRole("form"), "name")).toHaveValue("Lima");
  });

  it("guarda con un solo envío, cambia la tarjeta sin volver a pedir la lista y lo anuncia", async () => {
    const saved = { ...lima, name: "Lima Centro", phone: "", timezone: "UTC", code: "OTRO" };
    const api = mockApi({ [LIST]: list(), [EDIT("b2")]: { status: 200, body: saved } });
    renderApp(ui());
    const form = await opened();
    fill(form, { name: "  Lima   Centro ", phone: "  ", timezone: " UTC " });
    const release = hold(api);
    send(form);
    fireEvent.submit(form); // Enter y un segundo clic mientras se envía: un solo cambio
    send(form);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" })); // ni se cierra
    await tick();
    expect(within(form).getByRole("button", { name: "Guardando…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "PATCH")).toHaveLength(1);
    const [url, init] = calls(api, "PATCH")[0]!;
    expect(String(url)).toBe("/api/v1/o/acme/branches/b2/"); // el id que dio la API
    // Sin espacios exteriores; ni el código ni el estado viajan.
    expect(body([url, init])).toEqual({
      name: "Lima   Centro",
      address: "Av. Wilson 1234",
      district: "",
      city: "Lima",
      phone: "",
      timezone: "UTC",
    });
    const lines = [...card("Lima Centro").querySelectorAll("p")].map((p) => p.textContent);
    expect(lines.slice(0, 4)).toEqual([
      "Lima CentroB2", // el código de la fila no lo toca la respuesta
      "Av. Wilson 1234, Lima",
      "Zona horaria: UTC",
      "Activa",
    ]);
    const done = within(card("Lima Centro")).getByText("Sucursal «Lima Centro» guardada.");
    expect(done).toHaveAttribute("role", "status");
    expect(done.textContent).toBe("Sucursal «Lima Centro» guardada."); // el nombre guardado
    await waitFor(() => expect(trigger("Lima Centro")).toHaveFocus());
    expect(calls(api, "GET")).toHaveLength(1); // la lista no se vuelve a pedir
    expect(card("Cusco")).toHaveTextContent("Inactiva"); // solo cambia esa sucursal
    fireEvent.click(trigger("Lima Centro")); // al reabrir: lo guardado, y sin el anuncio
    expect(field(screen.getByRole("form"), "timezone")).toHaveValue("UTC");
    expect(screen.queryByText(/guardada/)).not.toBeInTheDocument();
  });

  it("sin nombre o sin zona horaria no envía nada: lo dice junto al campo", async () => {
    const api = mockApi({ [LIST]: list() });
    renderApp(ui());
    const form = await opened();
    fill(form, { name: "  ", timezone: "" });
    send(form);
    expect(field(form, "name")).toHaveAccessibleDescription("Escribe un nombre para la sucursal.");
    expect(field(form, "timezone")).toHaveAccessibleDescription("Escribe una zona horaria.");
    expect(field(form, "name")).toHaveFocus();
    await tick();
    expect(calls(api, "PATCH")).toHaveLength(0);
  });

  it("lo que la API no acepta se dice junto a su campo, y lo demás en el formulario", async () => {
    let reply: { status: number; body: unknown } = {
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { timezone: [{ code: "invalid" }] } },
    };
    const api = mockApi({ [LIST]: list(), [EDIT("b2")]: () => reply });
    renderApp(ui());
    const form = await opened();
    fill(form, { timezone: "Lima" });
    send(form);
    await waitFor(() =>
      expect(field(form, "timezone")).toHaveAccessibleDescription(/Esa zona horaria no existe/),
    );
    expect(field(form, "timezone")).toHaveFocus();
    expect(within(form).getAllByRole("alert")).toHaveLength(1);
    reply = { status: 403, body: { code: "PERMISSION_DENIED", message: "texto de la API" } };
    send(form);
    expect(
      await within(form).findByText("No tienes permiso para cambiar sucursales."),
    ).toHaveAttribute("role", "alert");
    expect(form).not.toHaveTextContent("texto de la API");
    reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    send(form);
    expect(await within(form).findByText(/Algo salió mal/)).toBeVisible();
    expect(field(form, "timezone")).toHaveValue("Lima"); // lo escrito no se pierde
    expect(calls(api, "PATCH")).toHaveLength(3);
    expect(card("Lima")).toHaveTextContent("Zona horaria: America/Lima"); // la tarjeta no cambia
  });

  it("sin red el intento falla y se dice: no queda en cola", async () => {
    const api = mockApi({ [LIST]: list(), [EDIT("b2")]: { status: 200, body: lima } });
    renderApp(ui());
    const form = await opened();
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    send(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("No hay conexión");
    onlineManager.setOnline(true);
    send(form); // con red, el mismo botón lo consigue
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "PATCH")).toHaveLength(2);
  });

  it("una lectura en vuelo no pisa lo recién guardado: se cancela, y la lista entera se repite", async () => {
    const saved = { ...lima, name: "Lima Centro" };
    let rows = branches;
    const api = mockApi({ [LIST]: () => list(rows), [EDIT("b2")]: { status: 200, body: saved } });
    const view = renderApp(ui());
    const form = await opened();
    fill(form, { name: "Lima Centro" });
    // Una relectura de la lista en vuelo, que responderá con el nombre de antes.
    let releaseRead = () => {};
    api.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (releaseRead = resolve));
      return new Response(JSON.stringify({ results: branches, next: null }), { status: 200 });
    });
    void view.client.refetchQueries();
    await tick();
    rows = [branches[0]!, saved, branches[2]!]; // lo que la API tiene tras guardar
    send(form);
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(card("Lima Centro")).toBeVisible();
    releaseRead(); // llega tarde: no devuelve «Lima» a la tarjeta
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3)); // y la lectura se repite
    await tick();
    expect(card("Lima Centro")).toBeVisible();
    expect(screen.queryByText("Lima", { selector: "span.font-medium" })).not.toBeInTheDocument();
  });

  it("con «Cargar más» en vuelo: se cancela, no pisa lo guardado y nada se vuelve a pedir", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [branches[0]!], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [lima], next: "def" } },
      [`${LIST}?cursor=def`]: list([branches[2]!]),
      [EDIT("b2")]: { status: 200, body: { ...lima, name: "Lima Centro" } },
    });
    renderApp(ui());
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await screen.findByText("Lima", { selector: "span.font-medium" });
    const form = await opened();
    fill(form, { name: "Lima Centro" });
    const more = screen.getByRole("button", { name: "Cargar más" });
    const release = hold(api); // la página 3 sale con la lista de antes de guardar
    fireEvent.click(more);
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3));
    send(form);
    more.focus(); // el usuario ya está en otra parte: al cerrarse no se le quita el foco
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    release();
    await tick();
    expect(card("Lima Centro")).toBeVisible(); // en la página 2, y lo que llega tarde no lo pisa
    expect(more).toHaveAttribute("aria-disabled", "false"); // hay que pulsarlo otra vez
    expect(calls(api, "GET")).toHaveLength(3); // un «Cargar más» cancelado no se repite
    expect(more).toHaveFocus();
  });

  it("si la API responde con otra sucursal, no se da por guardado: se dice y la lista se vuelve a pedir", async () => {
    const other = { ...branches[2]!, name: "Zeta" };
    const api = mockApi({ [LIST]: list(), [EDIT("b2")]: { status: 200, body: other } });
    renderApp(ui());
    const form = await opened();
    send(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("Algo salió mal");
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    expect(card("Lima")).toBeVisible();
    expect(screen.queryByText("Zeta")).not.toBeInTheDocument();
  });

  it("lo que se edita queda fijado al abrir, aunque la lista cambie debajo", async () => {
    let rows = branches;
    mockApi({ [LIST]: () => list(rows) });
    const view = renderApp(ui());
    const form = await opened();
    rows = [branches[0]!, { ...lima, name: "Lima B", district: "Cercado" }, branches[2]!];
    void view.client.refetchQueries();
    await screen.findByText("Lima B", { selector: "span.font-medium" }); // la tarjeta sigue a la lista
    expect(screen.getByRole("form", { name: "Editar Lima" })).toBe(form); // el formulario, no
    expect(field(form, "name")).toHaveValue("Lima");
    expect(field(form, "district")).toHaveValue(""); // un campo vacío seguiría a la fila
    expect(within(form).getByText("Editar Lima")).toBeVisible();
  });

  it("un 404 cierra el formulario, lo explica la lista y la vuelve a pedir; abrir otro retira el aviso", async () => {
    let rows = branches;
    const api = mockApi({
      [LIST]: () => list(rows),
      [EDIT("b2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const form = await opened();
    rows = [branches[0]!, branches[2]!]; // ya no está
    card("Lima").tabIndex = -1; // como la primera fila que trae «Cargar más», que admite el foco
    card("Lima").focus(); // el foco está en la tarjeta, no en el formulario: desaparece con ella
    fireEvent.submit(form);
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("La sucursal Lima ya no existe. Revisa la lista.");
    await waitFor(() =>
      expect(screen.queryByText("Lima", { selector: "span.font-medium" })).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    fireEvent.click(trigger("Cusco")); // otra acción: el aviso anterior ya no aplica
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("un 404 cierra el formulario aunque la sucursal siga en la lista, y no mueve el foco de otra parte", async () => {
    const api = mockApi({
      [LIST]: list(),
      [EDIT("b2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const form = await opened();
    send(form);
    const create = screen.getByRole("button", { name: "Crear sucursal" });
    create.focus(); // el usuario ya está en otra parte
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    expect(await screen.findByRole("alert")).toHaveTextContent("La sucursal Lima ya no existe");
    await tick();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(trigger()).toBeVisible(); // la tarjeta sigue: la lista aún la trae
    expect(create).toHaveFocus();
    fireEvent.click(create); // abrir «Crear sucursal» también retira el aviso
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("Enter mantenido no vuelve a pulsar: ni reabre el formulario ni reenvía", async () => {
    const api = mockApi({ [LIST]: list(), [EDIT("b2")]: { status: 500, body: { code: "X" } } });
    renderApp(ui());
    await screen.findByRole("list", { name: "Sucursales" });
    expect(fireEvent.keyDown(trigger(), { key: "Enter", repeat: true })).toBe(false);
    const form = await opened();
    send(form);
    await within(form).findByRole("alert");
    const held = fireEvent.keyDown(within(form).getByRole("button", { name: "Guardar" }), {
      key: "Enter",
      repeat: true,
    });
    expect(held).toBe(false);
    expect(fireEvent.keyDown(field(form, "name"), { key: "a", repeat: true })).toBe(true);
    expect(calls(api, "PATCH")).toHaveLength(1);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    fireEvent.click(trigger()); // al reabrir tras un fallo, el error no se arrastra
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
