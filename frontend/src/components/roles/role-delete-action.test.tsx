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
const DELETE = (id: string) => `DELETE /api/v1/o/acme/roles/${id}/`;
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
const roles = [
  role("r1", "Owner", { is_system: true, editable: false, members: 1 }),
  role("r2", "Caja"),
  role("r3", "Vendedor", { is_system: true }), // de plantilla: se edita, no se borra
  role("r4", "Mío", { editable: false }), // uno que tiene quien mira
  role("r5", "Bodega", { members: 2 }),
];
const list = (results: Role[] = roles) => ({ status: 200, body: { results, next: null } });
const done = { status: 204 };
const ui = (context = tenant("roles.view", "roles.manage")) => (
  <TenantProvider value={context}>
    <RolesList />
  </TenantProvider>
);
const names = () =>
  within(screen.getByRole("list", { name: "Roles" }))
    .getAllByText(/./, { selector: "span.font-medium" })
    .map((node) => node.textContent);
const calls = (api: ReturnType<typeof mockApi>, method: string) =>
  api.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
const tick = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 5)));
const trigger = (name = "Caja") => screen.getByRole("button", { name: `Borrar el rol ${name}` });
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
async function asked(name = "Caja") {
  await screen.findByRole("list", { name: "Roles" });
  const button = trigger(name);
  button.focus();
  fireEvent.click(button);
  return screen.getByRole("group", { name: `Borrar el rol ${name}` });
}
const confirm = (group: HTMLElement) => within(group).getByRole("button", { name: "Sí, borrar" });

beforeEach(() => {
  document.cookie = "csrftoken=t"; // con token: una escritura no pide antes el CSRF
});
afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("RoleDeleteAction", () => {
  it("se ofrece con el permiso en los roles editables que no son de plantilla, y nada se envía sin confirmar", async () => {
    const api = mockApi({ [LIST]: list() });
    const view = renderApp(ui(tenant("roles.view")));
    await screen.findByRole("list", { name: "Roles" });
    expect(screen.queryByRole("button", { name: /Borrar el rol/ })).not.toBeInTheDocument();
    view.unmount();
    renderApp(ui());
    await screen.findByRole("list", { name: "Roles" });
    const offered = screen.getAllByRole("button", { name: /Borrar el rol/ });
    expect(offered.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Borrar el rol Caja", // ni el Owner, ni una plantilla, ni uno propio
      "Borrar el rol Bodega", // con miembros se ofrece: la API explica por qué no
    ]);
    const group = await asked();
    expect(group).toHaveAccessibleDescription(
      "¿Borrar el rol Caja? Se borra con sus permisos y no se puede deshacer.",
    );
    expect(within(group).getByRole("button", { name: "Cancelar" })).toHaveFocus();
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("group", { name: /Borrar el rol/ })).not.toBeInTheDocument();
    expect(trigger()).toHaveFocus();
    await tick();
    expect(calls(api, "DELETE")).toHaveLength(0);
  });

  it("borra con un solo envío, quita la tarjeta sin volver a pedir la lista y lo anuncia en la lista", async () => {
    const api = mockApi({ [LIST]: list(), [DELETE("r2")]: done });
    renderApp(ui());
    const group = await asked();
    const release = hold(api);
    fireEvent.click(confirm(group));
    fireEvent.click(confirm(group)); // mientras se envía: una sola petición
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" })); // ni se cierra
    await tick();
    const busy = within(group).getByRole("button", { name: "Borrando…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    expect(names()).toContain("Caja"); // hasta que la API responde, sigue ahí
    release();
    await waitFor(() => expect(names()).toEqual(["Owner", "Vendedor", "Mío", "Bodega"]));
    expect(calls(api, "DELETE")).toHaveLength(1);
    expect(String(calls(api, "DELETE")[0]![0])).toBe("/api/v1/o/acme/roles/r2/"); // por su id
    expect(calls(api, "GET")).toHaveLength(1); // la lista no se vuelve a pedir
    expect(screen.getByText("Rol «Caja» borrado.")).toHaveAttribute("role", "status");
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus(); // la tarjeta ya no está
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(trigger("Bodega")); // otra acción: el anuncio anterior ya no aplica
    expect(screen.queryByText(/borrado/)).not.toBeInTheDocument();
  });

  it.each([
    [403, { code: "PERMISSION_DENIED" }, "No puedes borrar este rol"],
    [409, { code: "ROLE_IN_USE", message: "texto de la API" }, "Este rol tiene miembros"],
    [409, { code: "ROLE_IS_SYSTEM" }, "Un rol de plantilla no se borra."],
    [409, { code: "LAST_OWNER" }, "Debe quedar al menos un Owner activo"],
    [500, { code: "INTERNAL_ERROR" }, "Algo salió mal de nuestro lado"],
  ])(
    "un %s se explica en la tarjeta, por su código, y el mismo botón reintenta",
    async (status, body, text) => {
      const api = mockApi({ [LIST]: list(), [DELETE("r5")]: { status, body } });
      renderApp(ui());
      const group = await asked("Bodega");
      fireEvent.click(confirm(group));
      expect(await within(group).findByRole("alert")).toHaveTextContent(text);
      expect(group).not.toHaveTextContent("texto de la API");
      expect(names()).toContain("Bodega"); // nada cambió
      expect(screen.queryByText(/borrado/)).not.toBeInTheDocument();
      fireEvent.click(confirm(group)); // la marca de envío se soltó
      await waitFor(() => expect(calls(api, "DELETE")).toHaveLength(2));
      fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
      const again = await asked("Bodega");
      expect(within(again).queryByRole("alert")).not.toBeInTheDocument(); // al reabrir, sin el error
    },
  );

  it("sin red el intento falla y se dice: no queda en cola", async () => {
    const api = mockApi({ [LIST]: list(), [DELETE("r2")]: done });
    renderApp(ui());
    const group = await asked();
    onlineManager.setOnline(false);
    api.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(confirm(group));
    expect(await within(group).findByRole("alert")).toHaveTextContent("No hay conexión");
    expect(calls(api, "DELETE")).toHaveLength(1);
    expect(names()).toContain("Caja");
  });

  it.each([
    ["en la tarjeta", true],
    ["en otra parte", false],
  ])(
    "un 404 cierra la confirmación, vuelve a pedir la lista y lo explica fuera (foco %s)",
    async (_where, inside) => {
      let rows = roles;
      const api = mockApi({
        [LIST]: () => list(rows),
        [DELETE("r2")]: { status: 404, body: { code: "NOT_FOUND" } },
      });
      renderApp(ui());
      const group = await asked();
      rows = roles.filter((row) => row.id !== "r2"); // otra persona ya lo borró
      const elsewhere = trigger("Bodega");
      if (!inside) elsewhere.focus();
      fireEvent.click(confirm(group));
      await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
      await waitFor(() => expect(names()).not.toContain("Caja"));
      expect(screen.getByRole("alert")).toHaveTextContent(
        "El rol Caja ya no existía. Revisa la lista.",
      );
      expect(screen.queryByText(/borrado/)).not.toBeInTheDocument();
      expect(inside ? screen.getByRole("heading", { level: 1 }) : elsewhere).toHaveFocus();
    },
  );

  it("lo que se confirma queda fijado al abrir, y una lectura en vuelo no devuelve el rol", async () => {
    let rows = roles;
    const api = mockApi({ [LIST]: () => list(rows), [DELETE("r2")]: done });
    const view = renderApp(ui());
    const group = await asked();
    rows = roles.map((row) => (row.id === "r2" ? { ...row, name: "Caja B" } : row));
    void view.client.refetchQueries();
    await screen.findByText("Caja B", { selector: "span.font-medium" }); // la tarjeta sigue a la lista
    expect(screen.getByRole("group", { name: "Borrar el rol Caja" })).toBe(group); // la pregunta, no
    // Una relectura en vuelo, que responderá con el rol todavía en la lista.
    let releaseRead = () => {};
    api.mockImplementationOnce(async () => {
      await new Promise<void>((resolve) => (releaseRead = resolve));
      return new Response(JSON.stringify({ results: before, next: null }), { status: 200 });
    });
    void view.client.refetchQueries();
    await tick();
    const before = rows;
    rows = before.filter((row) => row.id !== "r2"); // lo que la API tiene tras borrar
    fireEvent.click(confirm(group));
    await waitFor(() => expect(names()).not.toContain("Caja B"));
    expect(screen.getByText("Rol «Caja» borrado.")).toBeVisible(); // el nombre que se confirmó
    releaseRead(); // llega tarde: no devuelve la tarjeta
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(4)); // y la lectura se repite
    await tick();
    expect(names()).toEqual(["Owner", "Vendedor", "Mío", "Bodega"]);
  });

  it("sin sesión va al login una vez y la confirmación sigue ocupada, sin error", async () => {
    const assign = vi.fn();
    const { origin } = window.location;
    vi.stubGlobal("location", { origin, pathname: "/o/acme/roles", search: "", assign });
    const api = mockApi({
      [LIST]: list(),
      [DELETE("r2")]: { status: 401, body: { code: "NOT_AUTHENTICATED" } },
    });
    render(
      <NextIntlClientProvider locale="es-PE" messages={messages} timeZone="America/Lima">
        <Providers>{ui()}</Providers>
      </NextIntlClientProvider>,
    );
    const group = await asked();
    fireEvent.click(confirm(group));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login?next=%2Fo%2Facme%2Froles"));
    await tick();
    fireEvent.click(within(group).getByRole("button", { name: "Borrando…" }));
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    await tick();
    expect(calls(api, "DELETE")).toHaveLength(1);
    expect(assign).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(names()).toContain("Caja");
  });

  it("una pulsación entre la respuesta y el render no reenvía, y un Enter mantenido no activa", async () => {
    const api = mockApi({
      [LIST]: list(),
      [DELETE("r2")]: { status: 500, body: { code: "INTERNAL_ERROR" } },
    });
    renderApp(ui());
    const group = await asked();
    const release = hold(api);
    fireEvent.click(confirm(group));
    await tick();
    const pressed = within(group).getByRole("button", { name: "Borrando…" });
    await act(async () => {
      release();
      for (let turn = 0; turn < 100; turn += 1) await Promise.resolve(); // llegó; aún sin render
      fireEvent.click(pressed);
    });
    await within(group).findByRole("alert");
    expect(calls(api, "DELETE")).toHaveLength(1);
    expect(fireEvent.keyDown(confirm(group), { key: "Enter", repeat: true })).toBe(false);
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    expect(fireEvent.keyDown(trigger(), { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(trigger(), { key: "Enter" })).toBe(true); // el primero sí
  });

  it("un 404 cierra la confirmación aunque el rol siga en la lista que llega", async () => {
    const api = mockApi({
      [LIST]: list(),
      [DELETE("r2")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    renderApp(ui());
    const group = await asked();
    fireEvent.click(confirm(group));
    await waitFor(() => expect(calls(api, "GET")).toHaveLength(2));
    expect(screen.queryByRole("group", { name: /Borrar el rol/ })).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("El rol Caja ya no existía");
    expect(trigger()).toBeVisible(); // la tarjeta sigue: la API de la lista aún lo trae
  });

  it("tras el 204 y antes de que la tarjeta se vaya, otra pulsación no reenvía", async () => {
    const api = mockApi({ [LIST]: list(), [DELETE("r2")]: done });
    renderApp(ui());
    const group = await asked();
    const release = hold(api);
    fireEvent.click(confirm(group));
    await tick();
    const pressed = within(group).getByRole("button", { name: "Borrando…" });
    await act(async () => {
      release();
      for (let turn = 0; turn < 100; turn += 1) {
        await Promise.resolve();
        fireEvent.click(pressed); // en cada paso entre la respuesta y el render
      }
    });
    await waitFor(() => expect(names()).not.toContain("Caja"));
    expect(calls(api, "DELETE")).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
