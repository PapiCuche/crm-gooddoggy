import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { onlineManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Contact, SelfContext } from "@/lib/api/model";
import { mockApi as mockRoutes, renderApp } from "@/test-utils";

import { ContactsList } from "./contacts-list";

const LIST = "GET /api/v1/o/acme/contacts/";
const CREATE = "POST /api/v1/o/acme/contacts/";
const MEMBERS = "GET /api/v1/o/acme/members/?limit=200";
const BRANCHES = "GET /api/v1/o/acme/branches/?limit=200";
const TAGS = "GET /api/v1/o/acme/tags/?limit=200";
// El filtro lee el catálogo de etiquetas (F4-12): vacío, salvo que el test diga otra cosa.
const mockApi = (routes: Parameters<typeof mockRoutes>[0]) =>
  mockRoutes({ [TAGS]: page([]), ...routes });
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
// Solo ve: sin `contacts.create`, como el Supervisor de la plantilla.
const viewer: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("contacts.view"),
};
const creator = { ...viewer, permissions: grants("contacts.view", "contacts.create") };
const contact = (id: string, changes: Partial<Contact> = {}): Contact => ({
  ...{ id, display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz" },
  ...{ primary_phone_e164: null, primary_email: null, document_type: null },
  ...{ document_number: null, owner_user_id: "u1", branch_id: null, source: "MANUAL" },
  ...{ created_by_user_id: "u1", created_at: "2026-10-23T10:00:00Z" },
  ...{ updated_at: "2026-10-23T10:00:00Z", version: 1, tags: [] },
  ...changes,
});
const rosa = contact("c1", {
  ...{ primary_phone_e164: "+51987654321", primary_email: "rosa@correo.pe", branch_id: "b1" },
});
const luis = contact("c2", { display_name: "Luis Paz", owner_user_id: "u2", branch_id: "b9" });
const nadie = contact("c3", { display_name: "Veterinaria Sol", owner_user_id: null });
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const member = (id: string, first_name: string, last_name: string, email: string) => ({
  ...{ id: `m-${id}`, status: "ACTIVE", joined_at: "2026-01-01T00:00:00Z", roles: [] },
  ...{ user: { id, email, first_name, last_name }, default_branch: null },
});
const branch = (id: string, name: string, code: string) => ({
  ...{ id, name, code, is_active: true, address: "", district: "", city: "", phone: "" },
  timezone: "America/Lima",
});

const screenOf = (tenant: SelfContext) =>
  renderApp(
    <TenantProvider value={tenant}>
      <ContactsList />
    </TenantProvider>,
  );
const posted = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init?.body)));
const form = () => screen.getByRole("form", { name: "Nuevo cliente" });
const field = (label: string | RegExp) => within(form()).getByLabelText(label);
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

describe("ContactsList", () => {
  it("enseña nombre, teléfono, correo, responsable y sucursal; el Supervisor (sin `contacts.create`) no crea", async () => {
    const api = mockApi({
      [LIST]: page([rosa, luis, nadie]),
      [MEMBERS]: page([member("u2", "Luis", "Ramos", "luis@acme.pe")]),
      [BRANCHES]: page([branch("b1", "Miraflores", "MIR")]),
    });
    screenOf({
      ...viewer,
      permissions: grants("contacts.view", "users.view", "organization.view"),
    });
    const list = await screen.findByRole("list", { name: "Clientes" });
    const [first, second, third] = within(list).getAllByRole("listitem");
    expect(first).toHaveTextContent("Rosa Díaz");
    expect(within(first!).getByText("+51987654321")).toHaveClass("font-mono");
    // 390 px: una columna; el nombre y el correo largos se parten en lugar de desbordar.
    expect(first).toHaveClass("grid", "sm:grid-cols-[minmax(0,1fr)_auto]");
    expect(first).not.toHaveClass("grid-cols-2");
    expect(within(first!).getByText("rosa@correo.pe")).toHaveClass("wrap-anywhere");
    expect(within(first!).getByText("Rosa Díaz")).toHaveClass("wrap-anywhere");
    // El nombre abre la ficha (F4-06), en cada fila.
    expect(within(first!).getByRole("link", { name: "Rosa Díaz" })).toHaveAttribute(
      "href",
      "/o/acme/clientes/c1",
    );
    expect(
      within(list)
        .getAllByRole("link")
        .map((link) => link.getAttribute("href")),
    ).toEqual(["/o/acme/clientes/c1", "/o/acme/clientes/c2", "/o/acme/clientes/c3"]);
    expect(first).toHaveTextContent("Teléfono: +51987654321");
    expect(first).toHaveTextContent("Correo: rosa@correo.pe");
    expect(within(first!).getByText("Responsable: tú")).toBeVisible();
    expect(await within(first!).findByText("Sucursal: Miraflores (MIR)")).toBeVisible();
    expect(await within(second!).findByText("Responsable: Luis Ramos")).toBeVisible();
    expect(second).not.toHaveTextContent(/sucursal|teléfono|correo/i); // b9: no la puede nombrar
    expect(within(third!).getByText("Sin responsable")).toBeVisible();
    expect(within(third!).getByText("Sin sucursal")).toBeVisible();
    expect(screen.getByText("3 clientes en la lista")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    expect(api).toHaveBeenCalledTimes(4); // con el catálogo de etiquetas del filtro
  });

  it("sin `users.view` ni `organization.view` no pide nada más; un directorio negado no rompe", async () => {
    const api = mockApi({ [LIST]: page([luis]) });
    screenOf(viewer);
    expect(await screen.findByText("Responsable: otro miembro")).toBeVisible();
    expect(api.mock.calls.map(([url]) => String(url)).sort()).toEqual([
      "/api/v1/o/acme/contacts/",
      "/api/v1/o/acme/tags/?limit=200",
    ]);
    vi.unstubAllGlobals();
    mockApi({
      [LIST]: page([luis]),
      [MEMBERS]: { status: 403, body: { code: "PERMISSION_DENIED" } },
    });
    screenOf({ ...viewer, permissions: grants("contacts.view", "users.view") });
    await waitFor(() => expect(screen.getAllByText("Responsable: otro miembro")).toHaveLength(2));
    expect(screen.queryAllByRole("alert")).toEqual([]);
  });

  it("el Vendedor de la plantilla crea y nombra sucursales, pero no pide el directorio", async () => {
    const api = mockApi({ [LIST]: page([luis]), [BRANCHES]: page([branch("b9", "Surco", "SUR")]) });
    screenOf({
      ...viewer,
      permissions: grants("organization.view", "contacts.view", "contacts.create"),
    });
    expect(await screen.findByText("Sucursal: Surco (SUR)")).toBeVisible();
    expect(screen.getByText("Responsable: otro miembro")).toBeVisible();
    expect(screen.getByRole("button", { name: "Crear cliente" })).toBeVisible();
    expect(api.mock.calls.map(([url]) => String(url))).not.toContain(
      "/api/v1/o/acme/members/?limit=200",
    );
  });

  it("sin `contacts.view` no pide directorio ni sucursales aunque pueda leerlos", async () => {
    const api = mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf({ ...viewer, permissions: grants("users.view", "organization.view") });
    expect(await screen.findByText(/No tienes permiso para ver los clientes/)).toBeVisible();
    expect(api.mock.calls.map(([url]) => String(url))).toEqual(["/api/v1/o/acme/contacts/"]);
  });

  it("mientras llega el directorio dice solo «Responsable»; lo lee entero, página a página", async () => {
    let release = () => {};
    const api = mockApi({
      [LIST]: page([luis]),
      [`${MEMBERS}&cursor=p2`]: page([member("u2", "", "", "luis@acme.pe")]),
    });
    const reply = api.getMockImplementation()!;
    api.mockImplementation(async (input, init) => {
      if (String(input) !== MEMBERS.slice(4)) return reply(input, init);
      await new Promise<void>((done) => (release = done)); // el directorio tarda
      return new Response(JSON.stringify({ results: [], next: "p2" }), { status: 200 });
    });
    screenOf({ ...viewer, permissions: grants("contacts.view", "users.view") });
    expect(await screen.findByText("Responsable")).toBeVisible();
    expect(screen.queryByText(/otro miembro/)).toBeNull();
    release();
    expect(await screen.findByText("Responsable: luis@acme.pe")).toHaveClass("wrap-anywhere");
  });

  it("pagina por cursor con «Cargar más»", async () => {
    const api = mockApi({
      [LIST]: page([rosa], "c2"),
      [`${LIST}?cursor=c2`]: page([luis]),
    });
    screenOf(viewer);
    await screen.findByText("Rosa Díaz");
    press("Cargar más");
    expect(await screen.findByText("Luis Paz")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Cargar más" })).toBeNull();
    expect(api).toHaveBeenCalledTimes(3);
  });

  it("cargando, vacío, error con reintento y sin permiso", async () => {
    let reply: { status: number; body: object } = { status: 500, body: {} };
    mockApi({ [LIST]: () => reply });
    const view = screenOf(viewer);
    expect(screen.getByText("Cargando clientes…")).toBeVisible();
    expect(await screen.findByText(/Algo salió mal de nuestro lado/)).toBeVisible();
    reply = page([]);
    press("Reintentar");
    expect(await screen.findByText("Todavía no tienes clientes en la lista.")).toBeVisible();
    view.unmount();
    reply = { status: 403, body: { code: "PERMISSION_DENIED" } };
    screenOf(creator);
    expect(await screen.findByText(/No tienes permiso para ver los clientes/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Crear cliente" })).toBeNull();
  });

  it("crea con solo lo escrito, un envío por pulsación; anuncia, devuelve el foco y relee la lista", async () => {
    let rows: Contact[] = [];
    const api = mockApi({
      [LIST]: () => page(rows),
      [CREATE]: () => {
        rows = [rosa];
        return { status: 201, body: rosa };
      },
    });
    screenOf(creator);
    await screen.findByText("Todavía no tienes clientes en la lista.");
    // La región viva existe antes del alta: un lector de pantalla anuncia el cambio.
    const live = screen.getAllByRole("status").find((node) => node.textContent === "");
    expect(live).toHaveClass("sr-only");
    press("Crear cliente");
    expect(field("Nombre")).toHaveFocus();
    const limits = { Nombre: 100, Apellido: 100, "Nombre para mostrar (opcional)": 200 };
    const more = { "Teléfono (opcional)": 32, "Correo (opcional)": 254 };
    for (const [label, max] of Object.entries({ ...limits, ...more })) {
      expect(field(label)).toHaveAttribute("maxlength", String(max));
    }
    expect(field("Teléfono (opcional)")).toHaveAccessibleDescription(/\+51 987 654 321 en Perú/);
    expect(field("Teléfono (opcional)")).toHaveAttribute("inputmode", "tel");
    expect(field("Teléfono (opcional)")).toHaveValue(""); // el prefijo no se impone
    expect(field("Correo (opcional)")).toHaveAttribute("inputmode", "email");
    const sources = [...(field("Origen") as HTMLSelectElement).options].map((o) => o.value);
    expect(sources).toEqual(["MANUAL", "WALK_IN", "REFERRAL", "INBOUND_MESSAGE", "CAMPAIGN"]);
    fill({ Nombre: " Rosa ", Apellido: "Díaz", "Teléfono (opcional)": "+51 987 654 321" });
    fill({ "Correo (opcional)": "rosa@correo.pe ", "Número de documento (opcional)": "12345678" });
    fill({ "Tipo de documento": "DNI", Origen: "WALK_IN" });
    const reply = api.getMockImplementation()!;
    let release = () => {};
    api.mockImplementationOnce(async (input, init) => {
      await new Promise<void>((done) => (release = done)); // el alta tarda: se suelta a mano
      return reply(input, init);
    });
    press("Crear cliente", form());
    fireEvent.submit(form()); // otra vez, enseguida
    press("Crear cliente", form());
    await waitFor(() => expect(posted(api)).toHaveLength(1));
    expect(within(form()).getByRole("button", { name: "Creando…" })).toBeVisible();
    fill({ "Tipo de documento": "RUC", Origen: "CAMPAIGN" }); // mientras se crea, no cambian
    expect(field("Tipo de documento")).toHaveValue("DNI");
    expect(field("Origen")).toHaveValue("WALK_IN");
    release();
    await waitFor(() => expect(live).toHaveTextContent("Cliente «Rosa Díaz» creado."));
    expect(await screen.findByText("Rosa Díaz")).toBeVisible(); // la lista se volvió a pedir
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Crear cliente" })).toHaveFocus(),
    );
    expect(posted(api)).toEqual([
      {
        ...{ source: "WALK_IN", first_name: "Rosa", last_name: "Díaz" },
        ...{ primary_phone_e164: "+51 987 654 321", primary_email: "rosa@correo.pe" },
        ...{ document_number: "12345678", document_type: "DNI" },
      },
    ]);
    // Lo mínimo: solo el nombre para mostrar y el origen, que siempre se ve elegido.
    press("Crear cliente");
    expect(live).toBeEmptyDOMElement(); // abrir retira el anuncio anterior
    expect(live).toHaveClass("sr-only");
    fill({ "Nombre para mostrar (opcional)": "Veterinaria Sol" });
    press("Crear cliente", form());
    await waitFor(() => expect(posted(api)).toHaveLength(2));
    expect(posted(api)[1]).toEqual({ source: "MANUAL", display_name: "Veterinaria Sol" });
  });

  it("los errores de la API van junto a su campo, por nombre; un número sin tipo no se envía", async () => {
    let reply: { status: number; body: object } = { status: 500, body: {} };
    const api = mockApi({ [LIST]: page([]), [CREATE]: () => reply });
    screenOf(creator);
    await screen.findByText("Todavía no tienes clientes en la lista.");
    press("Crear cliente");
    fill({ "Número de documento (opcional)": "123" });
    press("Crear cliente", form());
    expect(field("Número de documento (opcional)")).toHaveAccessibleDescription(
      "Elige el tipo de documento, o borra el número.",
    );
    expect(field("Número de documento (opcional)")).toHaveFocus();
    expect(posted(api)).toEqual([]);
    fill({ "Número de documento (opcional)": "", "Teléfono (opcional)": "987654321" });
    const bad = (...names: string[]) => ({
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: Object.fromEntries(names.map((n) => [n, ["x"]])) },
    });
    const again = async (next: typeof reply, check: () => void) => {
      reply = next;
      press("Crear cliente", form());
      await waitFor(check);
    };
    await again(bad("primary_phone_e164", "primary_email"), () =>
      expect(field("Teléfono (opcional)")).toHaveAccessibleDescription(/escribe \+ y el código/),
    );
    expect(field("Correo (opcional)")).toHaveAccessibleDescription("El correo no es válido.");
    expect(field("Teléfono (opcional)")).toHaveFocus(); // el primero con error
    await again(bad("display_name"), () =>
      expect(field("Nombre para mostrar (opcional)")).toHaveAccessibleDescription(
        /Falta un nombre/,
      ),
    );
    fill({ "Tipo de documento": "RUC" });
    await again(bad("document_type"), () =>
      expect(field("Número de documento (opcional)")).toHaveAccessibleDescription(
        "Escribe el número del documento, o elige «Sin documento».",
      ),
    );
    expect(posted(api).at(-1)).toEqual({
      ...{ source: "MANUAL", primary_phone_e164: "987654321", document_type: "RUC" },
    });
    fill({ "Número de documento (opcional)": "2010" });
    await again(bad("document_number"), () =>
      expect(field("Número de documento (opcional)")).toHaveAccessibleDescription(
        /no tiene la forma del tipo elegido/,
      ),
    );
    await again({ status: 403, body: { code: "PERMISSION_DENIED" } }, () =>
      expect(within(form()).getByRole("alert")).toHaveTextContent(
        "No tienes permiso para crear clientes.",
      ),
    );
    const held = within(form()).getByRole("button", { name: "Crear cliente" });
    expect(fireEvent.keyDown(held, { key: "Enter", repeat: true })).toBe(false);
    press("Cancelar", form());
    expect(screen.queryByRole("form")).toBeNull();
    expect(screen.getByRole("button", { name: "Crear cliente" })).toHaveFocus();
    press("Crear cliente"); // reabrir no trae el error anterior
    expect(within(form()).queryByRole("alert")).toBeNull();
  });

  it("sin red, el alta falla enseguida y se dice; no queda en cola", async () => {
    const api = mockApi({ [LIST]: page([]) });
    screenOf(creator);
    await screen.findByText("Todavía no tienes clientes en la lista.");
    press("Crear cliente");
    fill({ Nombre: "Rosa" });
    onlineManager.setOnline(false); // el navegador avisó de que no hay red
    try {
      api.mockRejectedValueOnce(new TypeError("sin red"));
      press("Crear cliente", form());
      expect(await within(form()).findByRole("alert")).toHaveTextContent("No hay conexión");
    } finally {
      onlineManager.setOnline(true);
    }
  });

  it("si la lista no vuelve tras crear, lo dice", async () => {
    let broken = false;
    mockApi({
      [LIST]: () => (broken ? { status: 500, body: {} } : page([])),
      [CREATE]: () => {
        broken = true;
        return { status: 201, body: nadie };
      },
    });
    screenOf(creator);
    await screen.findByText("Todavía no tienes clientes en la lista.");
    press("Crear cliente");
    fill({ "Nombre para mostrar (opcional)": "Veterinaria Sol" });
    press("Crear cliente", form());
    expect(
      await screen.findByText(/«Veterinaria Sol» creado, pero la lista no se pudo/),
    ).toBeVisible();
  });
});
