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
    within(form).getByRole("button", { name: "Guardar" }).focus();
    send(form);
    expect(within(form).getByRole("alert")).toHaveTextContent("Escribe un nombre para el rol.");
    expect(field(form, "Nombre")).toHaveFocus();
    expect(calls(api, "PATCH")).toHaveLength(0);
    fireEvent.input(field(form, "Nombre"), { target: { value: "X" } }); // al corregir, se retira
    expect(within(form).queryByRole("alert")).not.toBeInTheDocument();
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
  });

  it.each([
    ["en el formulario", true],
    ["en otra parte", false],
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
      if (inside) within(form).getByRole("button", { name: "Guardar" }).focus();
      else elsewhere.focus();
      send(form);
      await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
      await waitFor(() =>
        expect(screen.queryByText("Caja", { selector: ".font-medium" })).not.toBeInTheDocument(),
      );
      expect(screen.getByRole("alert")).toHaveTextContent(
        "El rol Caja ya no existe o había cambiado. Revisa la lista.",
      );
      expect(inside ? screen.getByRole("heading", { level: 1 }) : elsewhere).toHaveFocus();
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

  it("lo que se edita queda fijado al abrir, aunque la lista cambie debajo", async () => {
    let rows = roles;
    mockApi({ [LIST]: () => list(rows) });
    const view = renderApp(ui());
    const form = await opened();
    rows = [roles[0]!, { ...caja, name: "Caja B", description: "Otra" }, roles[2]!];
    void view.client.refetchQueries();
    await screen.findByText("Caja B", { selector: "span.font-medium" }); // la tarjeta sigue a la lista
    expect(screen.getByRole("form", { name: "Editar Caja" })).toBe(form); // el formulario, no
    expect(field(form, "Nombre")).toHaveValue("Caja");
    expect(field(form, "Descripción (opcional)")).toHaveValue("Cobra en tienda");
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
    fireEvent.click(within(form).getByRole("button", { name: "Cancelar" }));
    expect(fireEvent.keyDown(trigger(), { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(trigger(), { key: "Enter" })).toBe(true); // el primero sí
  });
});
