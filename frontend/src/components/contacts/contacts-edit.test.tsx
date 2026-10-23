import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { onlineManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Contact, SelfContext } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { ContactsList } from "./contacts-list";

const LIST = "/api/v1/o/acme/contacts/";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("contacts.view", ...codes),
});
const editor = tenant("contacts.update", "contacts.delete");
const contact = (id: string, changes: Partial<Contact> = {}): Contact => ({
  ...{ id, display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz" },
  ...{ primary_phone_e164: "+51987654321", primary_email: "rosa@correo.pe" },
  ...{ document_type: "DNI", document_number: "12345678", owner_user_id: "u1" },
  ...{ branch_id: null, source: "MANUAL", created_by_user_id: "u1" },
  ...{ created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-23T10:00:00Z", version: 1 },
  ...changes,
});
const rosa = contact("c1");
const luis = contact("c2", { display_name: "Luis Paz", first_name: "Luis", last_name: "Paz" });

type Answer = { status: number; body?: unknown };
const reply = ({ status, body }: Answer) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status });
// Una API de mentira con estado, como la real: la lista, `PATCH` con su `version` (409 si ya no
// es la vigente) y `DELETE`. Fuera de la lista (borrado o fuera del alcance), 404.
function server(initial: Contact[]) {
  let rows = initial;
  const api = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (method === "GET" && url === LIST) return reply({ status: 200, body: { results: rows } });
    const row = rows.find((one) => url === `${LIST}${one.id}/`);
    if (!row) return reply({ status: 404, body: { code: "NOT_FOUND" } });
    if (method === "DELETE") {
      rows = rows.filter((one) => one !== row);
      return reply({ status: 204 });
    }
    const { version, ...changes } = JSON.parse(String(init?.body));
    if (version !== row.version) {
      return reply({ status: 409, body: { code: "CONTACT_VERSION_CONFLICT" } });
    }
    const saved = { ...row, ...changes, version: row.version + 1 };
    rows = rows.map((one) => (one === row ? saved : one));
    return reply({ status: 200, body: saved });
  });
  vi.stubGlobal("fetch", api);
  return Object.assign(api, {
    rows: () => rows,
    drop: (id: string) => (rows = rows.filter((one) => one.id !== id)),
    // La próxima escritura responde esto en lugar de lo que diría el servidor.
    answerOnce: (answer: Answer) => api.mockImplementationOnce(async () => reply(answer)),
    // La próxima escritura pasa, pero la lista no vuelve después (ni al reintentar).
    listFailsAfterOnce: () => {
      api.mockImplementationOnce(api.getMockImplementation()!);
      api.mockImplementationOnce(async () => reply({ status: 500, body: {} }));
      api.mockImplementationOnce(async () => reply({ status: 500, body: {} }));
    },
    // La próxima petición tarda hasta que el test la suelta.
    holdOnce: () => {
      const real = api.getMockImplementation()!;
      let release = () => {};
      api.mockImplementationOnce(async (input, init) => {
        await new Promise<void>((done) => (release = done));
        return real(input, init);
      });
      return () => release();
    },
  });
}
const sent = (api: ReturnType<typeof server>, method: string) =>
  api.mock.calls.flatMap(([url, init]) =>
    init?.method === method
      ? [{ url: String(url), body: JSON.parse(String(init.body ?? "0")) }]
      : [],
  );

const screenOf = (who: SelfContext) =>
  renderApp(
    <TenantProvider value={who}>
      <ContactsList />
    </TenantProvider>,
  );
type Scope = ReturnType<typeof within>;
const form = (scope: Scope = screen) => scope.getByRole("form", { name: /^Editar el cliente/ });
const field = (label: string, scope: Scope = screen) => within(form(scope)).getByLabelText(label);
// Como una pulsación real: el botón recibe el foco antes del clic.
function press(name: string | RegExp, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
const fill = (values: Record<string, string>, scope: Scope = screen) => {
  for (const [label, value] of Object.entries(values)) {
    fireEvent.change(field(label, scope), { target: { value } });
  }
};
const live = () =>
  screen.getAllByRole("status").find((node) => !/en la lista/.test(node.textContent!));

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => vi.unstubAllGlobals());

describe("ContactsList: editar y borrar", () => {
  it("cada acción se ofrece con su permiso; el Supervisor (solo ver) no tiene ninguna", async () => {
    server([rosa, luis]);
    const view = screenOf(tenant()); // el Supervisor de la plantilla: `contacts.view`
    await screen.findByText("Luis Paz");
    expect(screen.queryByRole("button")).toBeNull();
    view.unmount();
    screenOf(tenant("contacts.update")); // con cualquier alcance
    await screen.findByText("Luis Paz");
    expect(screen.getByRole("button", { name: "Editar el cliente Luis Paz" })).toBeVisible();
    expect(screen.getAllByRole("button", { name: /^Editar/ })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /^Borrar/ })).toBeNull();
  });

  it("precarga la fila y envía solo lo que cambió con la versión, una vez por pulsación", async () => {
    const api = server([rosa, luis]);
    screenOf(editor);
    await screen.findByText("Rosa Díaz");
    press("Editar el cliente Rosa Díaz");
    expect(field("Nombre")).toHaveFocus();
    const preload = {
      ...{ Nombre: "Rosa", Apellido: "Díaz", "Nombre para mostrar": "Rosa Díaz" },
      ...{ "Teléfono (opcional)": "+51987654321", "Correo (opcional)": "rosa@correo.pe" },
      ...{ "Número de documento (opcional)": "12345678", "Tipo de documento": "DNI" },
    };
    for (const [label, value] of Object.entries(preload)) expect(field(label)).toHaveValue(value);
    fill({ "Nombre para mostrar": " Rosa Díaz Vet ", "Correo (opcional)": "rosa@vet.pe" });
    const release = api.holdOnce();
    press("Guardar", form());
    fireEvent.submit(form()); // otra vez, enseguida
    press("Guardar", form());
    await waitFor(() => expect(sent(api, "PATCH")).toHaveLength(1));
    expect(within(form()).getByRole("button", { name: "Guardando…" })).toBeVisible();
    release();
    await waitFor(() => expect(live()).toHaveTextContent("Cliente «Rosa Díaz Vet» guardado."));
    expect(screen.getByText("Rosa Díaz Vet")).toBeVisible(); // la lista se volvió a pedir
    expect(screen.getByRole("button", { name: "Editar el cliente Rosa Díaz Vet" })).toHaveFocus();
    expect(sent(api, "PATCH")).toEqual([
      {
        url: `${LIST}c1/`,
        body: { display_name: "Rosa Díaz Vet", primary_email: "rosa@vet.pe", version: 1 },
      },
    ]);
  });

  it("vacío borra lo opcional; sin cambios no envía; tipo y número van juntos", async () => {
    const api = server([rosa]);
    screenOf(editor);
    await screen.findByText("Rosa Díaz");
    press("Editar el cliente Rosa Díaz");
    press("Guardar", form()); // nada cambió: se cierra sin escribir
    expect(screen.queryByRole("form")).toBeNull();
    expect(screen.getByRole("button", { name: "Editar el cliente Rosa Díaz" })).toHaveFocus();
    press("Editar el cliente Rosa Díaz");
    fill({ "Número de documento (opcional)": "" }); // el tipo sigue: falta el número
    press("Guardar", form());
    expect(field("Número de documento (opcional)")).toHaveAccessibleDescription(
      "Escribe el número del documento, o elige «Sin documento».",
    );
    fill({ "Tipo de documento": "", "Número de documento (opcional)": "123" });
    press("Guardar", form());
    expect(field("Número de documento (opcional)")).toHaveAccessibleDescription(
      "Elige el tipo de documento, o borra el número.",
    );
    expect(sent(api, "PATCH")).toEqual([]);
    fill({ "Número de documento (opcional)": "" });
    fill({ Apellido: "", "Teléfono (opcional)": "", "Correo (opcional)": "" });
    press("Guardar", form());
    await waitFor(() => expect(sent(api, "PATCH")).toHaveLength(1));
    expect(sent(api, "PATCH")[0]!.body).toEqual({
      ...{ last_name: "", primary_phone_e164: null, primary_email: null },
      ...{ document_number: null, document_type: null, version: 1 },
    });
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(screen.queryByText("rosa@correo.pe")).toBeNull();
  });

  it("dos pestañas con el mismo cliente: la segunda recibe el aviso, se recarga y no pisa", async () => {
    const api = server([rosa]);
    const first = within(screenOf(editor).container);
    const tab2 = screenOf(editor);
    const second = within(tab2.container);
    await first.findByText("Rosa Díaz");
    await second.findByText("Rosa Díaz");
    press("Editar el cliente Rosa Díaz", second.getByRole("list"));
    press("Editar el cliente Rosa Díaz", first.getByRole("list"));
    fill({ "Teléfono (opcional)": "+51911111111" }, first);
    press("Guardar", form(first));
    await waitFor(() => expect(first.queryByRole("form")).toBeNull());
    await tab2.client.invalidateQueries(); // al volver a la pestaña, la lista se relee debajo
    await second.findByText("+51911111111"); // la fila ya lo muestra; el formulario, no
    fill({ "Nombre para mostrar": "Rosa D." }, second); // abierta con la versión 1
    press("Guardar", form(second));
    // El formulario se monta de nuevo con lo recargado: se busca otra vez cada vez.
    await waitFor(() =>
      expect(within(form(second)).getByRole("alert")).toHaveTextContent(
        "Otra persona cambió este cliente mientras lo editabas. Ahora ves lo que guardó",
      ),
    );
    // Recargada con lo de la primera; lo escrito en la segunda no se envió otra vez.
    expect(field("Teléfono (opcional)", second)).toHaveValue("+51911111111");
    expect(field("Nombre para mostrar", second)).toHaveValue("Rosa Díaz");
    fill({ "Nombre para mostrar": "Rosa D." }, second); // repite su cambio: ahora con la 2
    press("Guardar", form(second));
    await waitFor(() => expect(second.queryByRole("form")).toBeNull());
    expect(sent(api, "PATCH").map(({ body }) => body)).toEqual([
      { primary_phone_e164: "+51911111111", version: 1 },
      { display_name: "Rosa D.", version: 1 },
      { display_name: "Rosa D.", version: 2 },
    ]);
    expect(api.rows()[0]).toMatchObject({ display_name: "Rosa D.", version: 3 });
    expect(api.rows()[0]).toHaveProperty("primary_phone_e164", "+51911111111");
  });

  it("409 sin la lista nueva: lo dice y no recarga; 404: lo explica la lista, que vuelve", async () => {
    const api = server([rosa, luis]);
    screenOf(editor);
    await screen.findByText("Rosa Díaz");
    press("Editar el cliente Rosa Díaz");
    fill({ Nombre: "Rosita" });
    api.answerOnce({ status: 409, body: { code: "CONTACT_VERSION_CONFLICT" } });
    api.answerOnce({ status: 500, body: {} }); // la lista no vuelve, ni al reintentar
    api.answerOnce({ status: 500, body: {} });
    press("Guardar", form());
    expect(await within(form()).findByRole("alert")).toHaveTextContent(
      "no se pudo cargar lo nuevo. Recarga la página",
    );
    expect(field("Nombre")).toHaveValue("Rosita");
    api.drop("c1"); // otra pestaña lo borró, o salió de su alcance
    press("Guardar", form());
    expect(
      await screen.findByText(
        "«Rosa Díaz» ya no está entre los clientes que puedes cambiar: se borró o quedó fuera de tu alcance. La lista se volvió a cargar.",
      ),
    ).toHaveAttribute("role", "alert");
    expect(screen.queryByRole("form")).toBeNull();
    await waitFor(() => expect(screen.queryByText("Rosa Díaz")).toBeNull());
    expect(screen.getByRole("heading", { name: "Clientes" })).toHaveFocus();
    press("Editar el cliente Luis Paz"); // abrir retira el aviso
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("los errores de la API van junto a su campo; sin permiso, en el formulario; sin red, enseguida", async () => {
    const api = server([rosa]);
    screenOf(editor);
    await screen.findByText("Rosa Díaz");
    press("Editar el cliente Rosa Díaz");
    fill({ "Correo (opcional)": "rosa@", "Teléfono (opcional)": "987" });
    const bad = (...names: string[]) => ({
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: Object.fromEntries(names.map((n) => [n, ["x"]])) },
    });
    api.answerOnce(bad("primary_phone_e164", "primary_email"));
    press("Guardar", form());
    await waitFor(() =>
      expect(field("Correo (opcional)")).toHaveAccessibleDescription("El correo no es válido."),
    );
    expect(field("Teléfono (opcional)")).toHaveFocus();
    api.answerOnce(bad("document_type"));
    press("Guardar", form());
    await waitFor(() =>
      expect(field("Número de documento (opcional)")).toHaveAccessibleDescription(
        "Escribe el número del documento, o elige «Sin documento».",
      ),
    );
    api.answerOnce({ status: 403, body: { code: "PERMISSION_DENIED" } });
    press("Guardar", form());
    expect(
      await within(form()).findByText("No tienes permiso para editar clientes."),
    ).toHaveAttribute("role", "alert");
    onlineManager.setOnline(false);
    try {
      api.mockRejectedValueOnce(new TypeError("sin red"));
      press("Guardar", form());
      expect(await within(form()).findByRole("alert")).toHaveTextContent("No hay conexión");
    } finally {
      onlineManager.setOnline(true);
    }
    expect(fireEvent.keyDown(field("Nombre"), { key: "Enter", repeat: true })).toBe(false);
  });

  it("borrar pide confirmación con el nombre y el foco en «Cancelar»; 204 relee y anuncia", async () => {
    const api = server([rosa, luis]);
    screenOf(editor);
    await screen.findByText("Rosa Díaz");
    press("Borrar el cliente Rosa Díaz");
    const ask = screen.getByRole("group", { name: "Borrar el cliente Rosa Díaz" });
    expect(ask).toHaveAccessibleDescription(
      "¿Borrar el cliente «Rosa Díaz»? Deja de aparecer en la lista.",
    );
    expect(within(ask).getByRole("button", { name: "Cancelar" })).toHaveFocus();
    press("Cancelar", ask);
    expect(screen.queryByRole("group")).toBeNull();
    expect(screen.getByRole("button", { name: "Borrar el cliente Rosa Díaz" })).toHaveFocus();
    expect(sent(api, "DELETE")).toEqual([]);
    press("Borrar el cliente Rosa Díaz");
    const release = api.holdOnce();
    const confirm = screen.getByRole("group");
    press("Borrar", confirm);
    press("Borrar", confirm); // otra vez, enseguida
    expect(await within(confirm).findByRole("button", { name: "Borrando…" })).toBeVisible();
    release();
    await waitFor(() => expect(live()).toHaveTextContent("Cliente «Rosa Díaz» borrado."));
    expect(screen.queryByText("Rosa Díaz")).toBeNull();
    expect(screen.getByRole("heading", { name: "Clientes" })).toHaveFocus();
    expect(sent(api, "DELETE")).toEqual([{ url: `${LIST}c1/`, body: 0 }]);
  });

  it("borrar: un 404 se explica en la lista; sin permiso, en la confirmación", async () => {
    const api = server([rosa, luis]);
    screenOf(editor);
    await screen.findByText("Luis Paz");
    press("Borrar el cliente Luis Paz");
    api.answerOnce({ status: 403, body: { code: "PERMISSION_DENIED" } });
    press("Borrar", screen.getByRole("group"));
    expect(await within(screen.getByRole("group")).findByRole("alert")).toHaveTextContent(
      "No tienes permiso para borrar clientes.",
    );
    api.drop("c2");
    press("Borrar", screen.getByRole("group"));
    expect(
      await screen.findByText(/«Luis Paz» ya no está entre los clientes que puedes cambiar/),
    ).toHaveAttribute("role", "alert");
    expect(screen.queryByRole("group")).toBeNull();
    await waitFor(() => expect(screen.queryByText("Luis Paz")).toBeNull());
  });

  it("la fila que desaparece bajo el formulario abierto sigue en su sitio, con lo escrito", async () => {
    const api = server([rosa, luis]);
    const view = screenOf(editor);
    await screen.findByText("Luis Paz");
    press("Editar el cliente Rosa Díaz");
    fill({ Nombre: "Rosita" });
    api.drop("c1"); // otra pestaña lo borró, o salió de su alcance
    await view.client.invalidateQueries(); // la lista se relee debajo, sin la fila
    expect(await within(form()).findByRole("alert")).toHaveTextContent(
      "«Rosa Díaz» ya no está entre los clientes que puedes cambiar: se borró o quedó fuera de tu alcance. Lo que escribiste sigue aquí",
    );
    expect(field("Nombre")).toHaveValue("Rosita");
    expect(field("Nombre")).toHaveFocus();
    const [first] = screen.getAllByRole("listitem");
    expect(within(first!).getByRole("form")).toBeVisible(); // en su sitio, no al final
    press("Cancelar", form());
    expect(screen.getByText(/«Rosa Díaz» ya no está.*La lista se volvió a cargar/)).toHaveAttribute(
      "role",
      "alert",
    );
    expect(screen.queryByRole("form")).toBeNull();
    expect(screen.queryByText("Rosa Díaz")).toBeNull();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Clientes" })).toHaveFocus();
    expect(sent(api, "PATCH")).toEqual([]);
  });

  it("guardado sin la lista nueva: la fila ya lleva lo guardado y editar otra vez no da 409", async () => {
    const api = server([rosa]);
    screenOf(editor);
    await screen.findByText("Rosa Díaz");
    press("Editar el cliente Rosa Díaz");
    fill({ "Nombre para mostrar": "Rosa D." });
    api.listFailsAfterOnce();
    press("Guardar", form());
    await waitFor(() =>
      expect(live()).toHaveTextContent(
        "Cliente «Rosa D.» guardado, pero la lista no se pudo actualizar.",
      ),
    );
    press("Editar el cliente Rosa D."); // la fila ya lo dice, con la versión nueva
    expect(field("Nombre para mostrar")).toHaveValue("Rosa D.");
    fill({ "Correo (opcional)": "rosa@vet.pe" });
    press("Guardar", form());
    await waitFor(() => expect(live()).toHaveTextContent("Cliente «Rosa D.» guardado."));
    expect(screen.queryByRole("form")).toBeNull();
    expect(sent(api, "PATCH").map(({ body }) => body)).toEqual([
      { display_name: "Rosa D.", version: 1 },
      { primary_email: "rosa@vet.pe", version: 2 },
    ]);
  });

  it("borrar sin la lista nueva: la confirmación se cierra y lo dice", async () => {
    const api = server([rosa, luis]);
    screenOf(editor);
    await screen.findByText("Rosa Díaz");
    press("Borrar el cliente Rosa Díaz");
    api.listFailsAfterOnce();
    press("Borrar", screen.getByRole("group"));
    await waitFor(() =>
      expect(live()).toHaveTextContent(
        "Cliente «Rosa Díaz» borrado, pero la lista no se pudo actualizar. Recarga la página.",
      ),
    );
    expect(screen.queryByRole("group")).toBeNull();
    expect(screen.getByRole("heading", { name: "Clientes" })).toHaveFocus();
    expect(sent(api, "DELETE")).toHaveLength(1);
  });
});
