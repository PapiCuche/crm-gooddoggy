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
const EDIT = (id: string) => `PATCH /api/v1/o/acme/roles/${id}/`;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const role = (id: string, name: string, extra: Partial<Role> = {}): Role => ({
  id,
  code: `c-${id}`, // distinto del id: la ruta pide el id
  name,
  description: "",
  is_system: false,
  permissions: [],
  members: 0,
  editable: true,
  ...extra,
});
const caja = role("r2", "Caja", {
  description: "Cobra en tienda",
  members: 3,
  permissions: [{ code: "roles.view", scope: null }],
});
const roles = [
  role("r1", "Owner", { is_system: true, editable: false, members: 1 }),
  caja,
  role("r3", "Vendedor", { is_system: true }), // de plantilla, pero editable: se renombra
];
const list = (results: Role[] = roles) => ({ status: 200, body: { results, next: null } });
const ui = (context = tenant("roles.view", "roles.manage")) => (
  <TenantProvider value={context}>
    <RolesList />
  </TenantProvider>
);
const card = (name: string) =>
  screen.getByText(name, { selector: ".font-medium" }).closest("li") as HTMLElement;
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const body = (call?: unknown[]) => JSON.parse(String((call?.[1] as RequestInit).body));
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const trigger = (name = "Caja") => screen.getByRole("button", { name: `Editar el rol ${name}` });
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
async function opened(name = "Caja") {
  await screen.findByRole("list", { name: "Roles" });
  fireEvent.click(trigger(name));
  return screen.getByRole("form", { name: `Editar ${name}` });
}
function fill(form: HTMLElement, name: string, description?: string) {
  fireEvent.input(field(form, "Nombre"), { target: { value: name } });
  if (description !== undefined) {
    fireEvent.input(field(form, "Descripción (opcional)"), { target: { value: description } });
  }
}
const send = (form: HTMLElement) =>
  fireEvent.click(within(form).getByRole("button", { name: "Guardar" }));

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("RoleEditAction", () => {
  it("se ofrece con el permiso y en los roles editables; abrir enseña lo que hay y no envía nada", async () => {
    const api = mockApi({ [LIST]: list() });
    const view = renderApp(ui(tenant("roles.view")));
    await screen.findByRole("list", { name: "Roles" });
    expect(screen.queryByRole("button", { name: /Editar el rol/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    await screen.findByRole("list", { name: "Roles" });
    const offered = screen.getAllByRole("button", { name: /Editar el rol/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Editar el rol Caja", // el Owner no: la API lo marca como no editable
      "Editar el rol Vendedor", // una plantilla sí: decide `editable`, no `is_system`
    ]);
    const form = await opened();
    expect(field(form, "Nombre")).toHaveFocus();
    expect(field(form, "Nombre")).toHaveValue("Caja");
    expect(field(form, "Nombre")).toHaveAttribute("maxlength", "100");
    expect(field(form, "Nombre")).toHaveAccessibleDescription(/No puede leerse igual/);
    expect(field(form, "Descripción (opcional)")).toHaveValue("Cobra en tienda");
    expect(field(form, "Descripción (opcional)")).toHaveAttribute("maxlength", "255");
    fill(form, "A medias");
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    await tick(); // «Cancelar» no es el botón de envío del formulario
    expect(calls(api, "PATCH")).toHaveLength(0);
    fireEvent.click(trigger()); // al reabrir, lo del rol: lo cancelado no se arrastra
    expect(field(screen.getByRole("form"), "Nombre")).toHaveValue("Caja");
  });

  it("guarda con un solo envío, cambia la tarjeta sin volver a pedir la lista y lo anuncia", async () => {
    const saved = { ...caja, name: "Caja Fuerte", description: "" };
    const api = mockApi({ [LIST]: list(), [EDIT("r2")]: { status: 200, body: saved } });
    renderApp(ui());
    const form = await opened();
    fill(form, "  Caja   Fuerte ", "  ");
    const release = hold(api);
    send(form);
    fireEvent.submit(form); // Enter y un segundo clic mientras se envía: un solo cambio
    send(form);
    fireEvent.input(field(form, "Nombre"), { target: { value: "Otra cosa" } }); // ni al escribir
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" })); // ni se cierra
    await tick();
    expect(within(form).getByRole("button", { name: "Guardando…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(form).toHaveAttribute("aria-busy", "true");
    expect(within(form).getByText("Cancelar")).toHaveAttribute("aria-disabled", "true");
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(calls(api, "PATCH")).toHaveLength(1);
    const [url, init] = calls(api, "PATCH")[0]!;
    expect(String(url)).toBe("/api/v1/o/acme/roles/r2/"); // el id del rol, no su código
    // Sin espacios exteriores; lo demás lo decide la API, que responde con lo guardado.
    expect(body([url, init])).toEqual({ name: "Caja   Fuerte", description: "" });
    expect(card("Caja Fuerte")).toHaveTextContent("3 miembros");
    expect(card("Caja Fuerte")).not.toHaveTextContent("Cobra en tienda");
    expect(screen.queryByText("Caja", { selector: ".font-medium" })).not.toBeInTheDocument();
    expect(within(card("Caja Fuerte")).getByText("Rol «Caja Fuerte» guardado.")).toHaveAttribute(
      "role",
      "status",
    );
    await waitFor(() => expect(trigger("Caja Fuerte")).toHaveFocus());
    expect(calls(api, "GET")).toHaveLength(1); // la lista no se vuelve a pedir
    expect(card("Vendedor")).toHaveTextContent("De plantilla"); // solo cambia ese rol
    fireEvent.click(trigger("Caja Fuerte")); // al reabrir: lo guardado, y sin el anuncio
    expect(field(screen.getByRole("form"), "Nombre")).toHaveValue("Caja Fuerte");
    expect(screen.queryByText(/guardado/)).not.toBeInTheDocument();
  });

  it("sin nombre no envía nada: lo dice junto al campo y lleva el foco a él", async () => {
    const api = mockApi({ [LIST]: list(), [EDIT("r2")]: { status: 200, body: caja } });
    renderApp(ui());
    const form = await opened();
    fill(form, "   ");
    field(form, "Descripción (opcional)").focus(); // Enter desde el otro campo
    send(form);
    expect(within(form).getByRole("alert")).toHaveTextContent("Escribe un nombre para el rol.");
    expect(field(form, "Nombre")).toHaveFocus();
    expect(calls(api, "PATCH")).toHaveLength(0);
    fireEvent.input(field(form, "Nombre"), { target: { value: "X" } }); // al corregir, se retira
    expect(within(form).queryByRole("alert")).not.toBeInTheDocument();
    fill(form, " ");
    send(form);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    fireEvent.click(trigger()); // y lo que faltaba no se arrastra a la siguiente apertura
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
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
      const api = mockApi({ [LIST]: list(), [EDIT("r2")]: { status, body: answer } });
      renderApp(ui());
      const form = await opened();
      fill(form, "Owner", "Como el de plantilla");
      const submit = within(form).getByRole("button", { name: "Guardar" });
      submit.focus();
      send(form);
      expect(await within(form).findByRole("alert")).toHaveTextContent(text);
      expect(field(form, label)).toHaveAttribute("aria-invalid", "true");
      // El error del nombre lleva el foco al nombre; el de la descripción lo deja donde estaba.
      await waitFor(() => expect(label === "Nombre" ? field(form, label) : submit).toHaveFocus());
      expect(field(form, "Nombre")).toHaveValue("Owner");
      expect(field(form, "Descripción (opcional)")).toHaveValue("Como el de plantilla");
      expect(card("Caja")).toHaveTextContent("Cobra en tienda"); // la tarjeta no cambió
      const release = hold(api);
      send(form); // el mismo botón reintenta: la marca de envío se soltó
      fireEvent.input(field(form, "Nombre"), { target: { value: "Otro" } }); // y escribir no la suelta
      send(form);
      await tick();
      release();
      await within(form).findByRole("alert");
      expect(calls(api, "PATCH")).toHaveLength(2);
    },
  );

  it.each([
    [403, { code: "PERMISSION_DENIED" }, "No puedes cambiar este rol"],
    [409, { code: "LAST_OWNER" }, "Debe quedar al menos un Owner activo"],
    [400, { code: "VALIDATION_ERROR", fields: { otro: [] } }, "Algo salió mal de nuestro lado"],
    [500, { code: "INTERNAL_ERROR", message: "texto de la API" }, "Algo salió mal de nuestro lado"],
  ])(
    "un %s que no es de un campo se explica en el formulario, por su código",
    async (status, answer, text) => {
      const api = mockApi({ [LIST]: list(), [EDIT("r2")]: { status, body: answer } });
      renderApp(ui());
      const form = await opened();
      fill(form, "Caja Fuerte");
      send(form);
      expect(await within(form).findByRole("alert")).toHaveTextContent(text);
      expect(form).not.toHaveTextContent("texto de la API");
      expect(field(form, "Nombre")).not.toHaveAttribute("aria-invalid");
      expect(field(form, "Nombre")).toHaveValue("Caja Fuerte");
      fireEvent.input(field(form, "Descripción (opcional)"), { target: { value: "Otra" } });
      // Al corregir, el error se retira.
      await waitFor(() => expect(within(form).queryByRole("alert")).not.toBeInTheDocument());
      send(form);
      await waitFor(() => expect(calls(api, "PATCH")).toHaveLength(2));
    },
  );

  it("sin red el intento falla y se dice: no queda en cola", async () => {
    const api = mockApi({ [LIST]: list(), [EDIT("r2")]: { status: 200, body: caja } });
    renderApp(ui());
    const form = await opened();
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    send(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(calls(api, "PATCH")).toHaveLength(1);
    // Sin tocar nada se envían igualmente los dos campos.
    expect(body(calls(api, "PATCH")[0])).toEqual({ name: "Caja", description: "Cobra en tienda" });
  });

  it.each([
    ["en el formulario", true],
    ["en otra parte", false],
    ["en ninguna parte", null], // un clic que no enfoca el botón (Safari)
    ["en el panel de permisos de la misma tarjeta", "card"], // que desaparece con ella
  ])(
    "un 404 cierra el formulario, vuelve a pedir la lista y lo explica fuera (foco %s)",
    async (_where, inside) => {
      let rows = roles;
      const api = mockApi({
        [LIST]: () => list(rows),
        [EDIT("r2")]: { status: 404, body: { code: "NOT_FOUND" } },
      });
      renderApp(ui());
      const form = await opened();
      rows = [roles[0]!, roles[2]!]; // otra persona ya lo borró
      const elsewhere = trigger("Vendedor");
      if (inside === true) within(form).getByRole("button", { name: "Guardar" }).focus();
      else if (inside === null) (document.activeElement as HTMLElement).blur();
      else if (inside === "card") {
        fireEvent.click(screen.getByRole("button", { name: "Cambiar los permisos de Caja" }));
        within(card("Caja")).getByRole("button", { name: "Cerrar" }).focus();
      } else elsewhere.focus();
      send(form);
      await waitFor(() => expect(calls(api, "GET")).toHaveLength(inside === "card" ? 3 : 2));
      await waitFor(() =>
        expect(screen.queryByText("Caja", { selector: ".font-medium" })).not.toBeInTheDocument(),
      );
      expect(screen.getByRole("alert")).toHaveTextContent(
        "El rol Caja ya no existe o había cambiado. Revisa la lista.",
      );
      const heading = screen.getByRole("heading", { level: 1 });
      expect(inside === false ? elsewhere : heading).toHaveFocus();
      fireEvent.click(trigger("Vendedor")); // al abrir otro, el aviso ya no aplica
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );

  it("una lectura en vuelo no pisa lo recién guardado: se cancela, y la lista entera se repite", async () => {
    const saved = { ...caja, name: "Caja Fuerte" };
    let rows = roles;
    const api = mockApi({
      [LIST]: () => list(rows),
      [EDIT("r2")]: { status: 200, body: saved },
    });
    const view = renderApp(ui());
    const form = await opened();
    fill(form, "Caja Fuerte");
    // Una relectura de la lista en vuelo, que responderá con el nombre de antes.
    let releaseRead = () => {};
    api.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (releaseRead = resolve));
      return new Response(JSON.stringify({ results: roles, next: null }), { status: 200 });
    });
    void view.client.refetchQueries();
    await tick();
    rows = [roles[0]!, saved, roles[2]!]; // lo que la API tiene tras guardar
    send(form);
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    expect(card("Caja Fuerte")).toBeVisible();
    releaseRead(); // llega tarde: no devuelve «Caja» a la tarjeta
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3)); // y la lectura se repite
    await tick();
    expect(card("Caja Fuerte")).toBeVisible();
    expect(screen.queryByText("Caja", { selector: ".font-medium" })).not.toBeInTheDocument();
  });

  it("con «Cargar más» en vuelo: se cancela, no pisa lo guardado y nada se vuelve a pedir", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [roles[0]!], next: "abc" } },
      [`${LIST}?cursor=abc`]: { status: 200, body: { results: [caja], next: "def" } },
      [`${LIST}?cursor=def`]: list([roles[2]!]),
      [EDIT("r2")]: { status: 200, body: { ...caja, name: "Caja Fuerte" } },
    });
    renderApp(ui());
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await screen.findByText("Caja");
    const form = await opened();
    fill(form, "Caja Fuerte");
    const more = screen.getByRole("button", { name: "Cargar más" });
    const release = hold(api); // la página 3 sale con la lista de antes de guardar
    fireEvent.click(more);
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(3));
    send(form);
    more.focus(); // el usuario ya está en otra parte: al cerrarse el formulario no se le quita el foco
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    release();
    await tick();
    expect(card("Caja Fuerte")).toBeVisible(); // en la página 2, y lo que llega tarde no lo pisa
    expect(screen.queryByText("Caja", { selector: ".font-medium" })).not.toBeInTheDocument();
    expect(more).toHaveAttribute("aria-disabled", "false"); // hay que pulsarlo otra vez
    expect(calls(api, "GET")).toHaveLength(3); // un «Cargar más» cancelado no se repite
    expect(more).toHaveFocus();
  });

  it("una respuesta tardía no pisa lo que «Permisos» cambió mientras tanto en la tarjeta", async () => {
    const catalog = { status: 200, body: { results: [{ code: "users.view" }] } };
    const api = mockApi({
      [LIST]: list(),
      "GET /api/v1/o/acme/permissions/": catalog,
      "PUT /api/v1/o/acme/roles/r2/permissions/users.view/": { status: 204 },
      [EDIT("r2")]: { status: 200, body: { ...caja, name: "Caja Fuerte" } }, // lo de antes de conceder
    });
    renderApp(ui());
    const form = await opened();
    fireEvent.click(screen.getByRole("button", { name: "Cambiar los permisos de Caja" }));
    const grant = await screen.findByRole("button", { name: "Conceder Ver miembros a Caja" });
    fill(form, "Caja Fuerte");
    const release = hold(api); // la respuesta de guardar tarda más que la de conceder
    send(form);
    await tick();
    fireEvent.click(grant);
    await screen.findByRole("button", { name: "Retirar Ver miembros a Caja" });
    release();
    await waitFor(() => expect(screen.queryByRole("form")).not.toBeInTheDocument());
    const granted = within(card("Caja Fuerte")).getByRole("list", { name: /^Permisos de/ });
    expect(granted).toHaveTextContent("Ver rolesVer miembros");
  });

  it("si la API responde con otro rol, no se da por guardado: se dice y la lista se vuelve a pedir", async () => {
    const other = { ...roles[2]!, name: "Zeta" };
    const api = mockApi({ [LIST]: list(), [EDIT("r2")]: { status: 200, body: other } });
    renderApp(ui());
    const form = await opened();
    send(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("Algo salió mal");
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    expect(card("Caja")).toBeVisible();
    expect(screen.queryByText("Zeta")).not.toBeInTheDocument();
  });

  it("lo que se edita queda fijado al abrir, aunque la lista cambie debajo", async () => {
    let rows = roles;
    mockApi({ [LIST]: () => list(rows) });
    const view = renderApp(ui());
    const form = await opened();
    const empty = await opened("Vendedor"); // sin descripción: un campo vacío tampoco sigue a la lista
    const other = { name: "Caja B", description: "Otra" };
    rows = [roles[0]!, { ...caja, ...other }, { ...roles[2]!, description: "Otra" }];
    void view.client.refetchQueries();
    await screen.findByText("Caja B", { selector: "span.font-medium" }); // la tarjeta sigue a la lista
    expect(screen.getByRole("form", { name: "Editar Caja" })).toBe(form); // el formulario, no
    expect(field(form, "Nombre")).toHaveValue("Caja");
    expect(field(form, "Descripción (opcional)")).toHaveValue("Cobra en tienda");
    expect(within(form).getByText("Editar Caja")).toBeVisible();
    expect(field(empty, "Descripción (opcional)")).toHaveValue("");
  });

  it("sin sesión va al login una vez y el formulario sigue ocupado, sin error", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/roles", search: "", assign });
    const api = mockApi({
      [LIST]: list(),
      [EDIT("r2")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const form = await opened();
    send(form);
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Froles"));
    await tick();
    fireEvent.submit(form); // sigue ocupado hasta que cambia la página
    fireEvent.input(field(form, "Nombre"), { target: { value: "Otro" } });
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(calls(api, "PATCH")).toHaveLength(1);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Guardando…" })).toBeInTheDocument();
  });

  it("una pulsación entre la respuesta y el render no reenvía, y un Enter mantenido no activa", async () => {
    const api = mockApi({
      [LIST]: list(),
      [EDIT("r2")]: { status: 500, body: { code: "INTERNAL_ERROR" } },
    });
    renderApp(ui());
    const form = await opened();
    const release = hold(api);
    send(form);
    await tick();
    await act(async () => {
      release();
      for (let turn = 0; turn < 100; turn += 1) await Promise.resolve(); // llegó; aún sin render
      fireEvent.submit(form);
    });
    await within(form).findByRole("alert");
    expect(calls(api, "PATCH")).toHaveLength(1);
    const submit = within(form).getByRole("button", { name: "Guardar" });
    expect(fireEvent.keyDown(submit, { key: "Enter", repeat: true })).toBe(false);
    // Borrar o escribir con la tecla mantenida sí.
    expect(fireEvent.keyDown(field(form, "Nombre"), { key: "Backspace", repeat: true })).toBe(true);
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    expect(fireEvent.keyDown(trigger(), { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(trigger(), { key: "Enter" })).toBe(true); // el primero sí
    fireEvent.click(trigger()); // al reabrir tras un fallo, el error no se arrastra
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
