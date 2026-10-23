import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Contact, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ContactCard } from "./contact-card";

const ID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const CARD = `GET /api/v1/o/acme/contacts/${ID}/`;
const MEMBERS = "GET /api/v1/o/acme/members/?limit=200";
const BRANCHES = "GET /api/v1/o/acme/branches/?limit=200";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const viewer: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants("contacts.view"),
};
const all = { ...viewer, permissions: grants("contacts.view", "users.view", "organization.view") };
const rosa: Contact = {
  ...{ id: ID, display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz" },
  ...{ primary_phone_e164: "+51987654321", primary_email: "rosa@correo.pe" },
  ...{ document_type: "DNI", document_number: "12345678", owner_user_id: "u2" },
  ...{ branch_id: "b1", source: "WALK_IN", created_by_user_id: "u1" },
  ...{ created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-24T20:30:00Z", version: 3 },
};
// Una empresa sin datos: solo el nombre para mostrar, que no es de nadie ni de ninguna sucursal.
const sol: Contact = {
  ...rosa,
  ...{ display_name: "Veterinaria Sol", first_name: "", last_name: "", primary_email: null },
  ...{ primary_phone_e164: null, document_type: null, document_number: null },
  ...{ owner_user_id: null, branch_id: null, source: "IMPORT" },
};
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
const ok = (body: Contact) => ({ status: 200, body });
const denied = { status: 403, body: { code: "PERMISSION_DENIED" } };
const missing = { status: 404, body: { code: "NOT_FOUND" } };

const cardOf = (tenant: SelfContext) =>
  renderApp(
    <TenantProvider value={tenant}>
      <ContactCard contactId={ID} />
    </TenantProvider>,
  );
const urls = (api: ReturnType<typeof mockApi>) => api.mock.calls.map(([url]) => String(url));
// Lo que dice la ficha de cada dato: el texto de su `dd`, por su `dt`.
function value(label: string) {
  const term = screen.getAllByRole("term").find((node) => node.textContent === label);
  return term ? term.nextElementSibling : null;
}
// `notFound()` lanza durante el render: React lo propaga como error de la ventana.
function notFoundThrown() {
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  return new Promise<unknown>((resolve) => {
    window.addEventListener("error", (event) => resolve(event.error), { once: true });
  }).finally(() => errors.mockRestore());
}

afterEach(() => vi.unstubAllGlobals());

describe("ContactCard", () => {
  it("enseña el perfil, el responsable, la sucursal, el origen y las fechas en «Resumen», su única pestaña", async () => {
    const api = mockApi({
      [CARD]: ok(rosa),
      [MEMBERS]: page([member("u2", "Luis", "Ramos", "luis@acme.pe")]),
      [BRANCHES]: page([branch("b1", "Miraflores", "MIR")]),
    });
    const view = cardOf(all);
    expect(screen.getByRole("status")).toHaveTextContent("Cargando el cliente…");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Ficha del cliente");
    expect(await screen.findByRole("heading", { level: 1, name: "Rosa Díaz" })).toBeVisible();
    const [tab, ...others] = within(screen.getByRole("tablist")).getAllByRole("tab");
    expect(others).toEqual([]); // ni notas ni etiquetas ni pestañas desactivadas
    expect(tab).toHaveAccessibleName("Resumen");
    expect(tab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tablist")).toHaveAccessibleName("Secciones de la ficha");
    const panel = screen.getByRole("tabpanel", { name: "Resumen" });
    expect(panel).toHaveAttribute("tabindex", "0"); // se llega con Tab desde la pestaña
    expect(tab).toHaveAttribute("aria-controls", panel.id);
    expect(within(panel).getByRole("region", { name: "Datos del cliente" })).toBeVisible();
    expect(within(panel).getByRole("region", { name: "En la organización" })).toBeVisible();
    expect(value("Nombre")).toHaveTextContent("Rosa");
    expect(value("Apellido")).toHaveTextContent("Díaz");
    expect(within(value("Teléfono") as HTMLElement).getByText("+51987654321")).toHaveClass(
      "font-mono",
    );
    expect(value("Correo")).toHaveTextContent("rosa@correo.pe");
    expect(value("Documento")).toHaveTextContent(/^DNI 12345678$/);
    expect(await screen.findByText("Luis Ramos")).toBe(value("Responsable"));
    expect(await screen.findByText("Miraflores (MIR)")).toBe(value("Sucursal"));
    expect(value("Origen")).toHaveTextContent("Visita a la tienda");
    const created = value("Creado")!.querySelector("time");
    expect(created).toHaveAttribute("dateTime", "2026-10-23T10:00:00Z");
    expect(created).toHaveTextContent(/^23 de octubre de 2026 a las 5:00/); // hora de Lima
    expect(value("Actualizado")).toHaveTextContent(/^24 de octubre de 2026 a las 3:30 p/);
    const back = within(screen.getByRole("navigation", { name: "Ruta de navegación" })).getByRole(
      "link",
    );
    expect(back).toHaveAccessibleName("Clientes");
    expect(back).toHaveAttribute("href", "/o/acme/clientes");
    expect(screen.queryByRole("button", { name: /editar|borrar/i })).toBeNull(); // solo lectura
    expect(urls(api).sort()).toEqual(
      [CARD, MEMBERS, BRANCHES].map((route) => route.slice(4)).sort(),
    );
    // Al salir no queda en memoria: cada entrada pregunta a la API.
    view.unmount();
    await waitFor(() => expect(view.client.getQueryCache().getAll()).toEqual([]));
  });

  it("sin `users.view` ni `organization.view` no pide nada más: «Otro miembro» y sin sucursal", async () => {
    const api = mockApi({ [CARD]: ok(rosa) });
    cardOf(viewer);
    expect(await screen.findByText("Otro miembro")).toBe(value("Responsable"));
    expect(value("Sucursal")).toBeNull(); // b1: no la puede nombrar, no se enseña
    expect(urls(api)).toEqual([CARD.slice(4)]);
  });

  it("«Tú», sin responsable, sin sucursal y lo que falta, con los orígenes que pone el sistema", async () => {
    mockApi({ [CARD]: ok({ ...rosa, owner_user_id: "u1", branch_id: null, source: "AI" }) });
    const view = cardOf(viewer);
    expect(await screen.findByText("Tú")).toBe(value("Responsable"));
    expect(value("Sucursal")).toHaveTextContent("Sin sucursal");
    expect(value("Origen")).toHaveTextContent("Asistente de IA");
    view.unmount();
    vi.unstubAllGlobals();
    mockApi({ [CARD]: ok(sol) });
    cardOf(all);
    expect(await screen.findByText("Sin responsable")).toBe(value("Responsable"));
    expect(value("Sucursal")).toHaveTextContent("Sin sucursal");
    for (const label of ["Nombre", "Apellido", "Teléfono", "Correo"]) {
      expect(value(label)).toHaveTextContent(/^Sin registrar$/);
    }
    expect(value("Documento")).toHaveTextContent("Sin documento");
    expect(value("Origen")).toHaveTextContent("Importación");
  });

  it("mientras llega el directorio no dice «Otro miembro»; un directorio negado no rompe", async () => {
    let release = () => {};
    const api = mockApi({ [CARD]: ok(rosa) });
    const reply = api.getMockImplementation()!;
    api.mockImplementation(async (input, init) => {
      if (String(input) !== MEMBERS.slice(4)) return reply(input, init);
      await new Promise<void>((done) => (release = done)); // el directorio tarda
      return new Response(JSON.stringify(page([member("u2", "", "", "luis@acme.pe")]).body));
    });
    const view = cardOf({ ...viewer, permissions: grants("contacts.view", "users.view") });
    await screen.findByRole("heading", { name: "Rosa Díaz" });
    expect(value("Responsable")).toHaveTextContent(/^…Cargando$/); // ni «Otro miembro»
    expect(within(value("Responsable") as HTMLElement).getByText("…")).toHaveAttribute(
      "aria-hidden",
    );
    expect(within(value("Responsable") as HTMLElement).getByText("Cargando")).toHaveClass(
      "sr-only",
    );
    release();
    expect(await screen.findByText("luis@acme.pe")).toBe(value("Responsable"));
    view.unmount();
    vi.unstubAllGlobals();
    mockApi({ [CARD]: ok(rosa), [MEMBERS]: denied });
    cardOf({ ...viewer, permissions: grants("contacts.view", "users.view") });
    expect(await screen.findByText("Otro miembro")).toBe(value("Responsable"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("fuera del alcance o inexistente (404) es la página de «no encontrada», sin enseñar nada", async () => {
    mockApi({ [CARD]: missing });
    const failure = notFoundThrown();
    cardOf(all);
    expect(await failure).toMatchObject({ message: "NEXT_NOT_FOUND" });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("un 404 con la ficha abierta (se borró o salió del alcance) también lleva a «no encontrada»", async () => {
    let reply: { status: number; body: unknown } = ok(rosa);
    const api = mockApi({ [CARD]: () => reply });
    const view = cardOf(viewer);
    await screen.findByRole("heading", { name: "Rosa Díaz" });
    reply = missing;
    const failure = notFoundThrown();
    void view.client.refetchQueries();
    expect(await failure).toMatchObject({ message: "NEXT_NOT_FOUND" });
    expect(api).toHaveBeenCalledTimes(2);
  });

  it("sin `contacts.view` (403) se cierra sin reintento ni más peticiones; un fallo luego no la reabre", async () => {
    let reply: { status: number; body: unknown } = denied;
    const api = mockApi({ [CARD]: () => reply });
    const view = cardOf({ ...viewer, permissions: grants("users.view", "organization.view") });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver los clientes de esta organización.",
    );
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(urls(api)).toEqual([CARD.slice(4)]);
    // Abierta, una negativa la cierra; un 500 después no la reabre con lo de antes.
    view.unmount();
    reply = ok(rosa);
    const open = cardOf(viewer);
    await screen.findByRole("heading", { name: "Rosa Díaz" });
    reply = denied;
    await open.client.refetchQueries();
    expect(await screen.findByRole("alert")).toHaveTextContent(/No tienes permiso/);
    expect(screen.queryByText("Rosa")).toBeNull();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Ficha del cliente");
    reply = { status: 500, body: {} };
    await open.client.refetchQueries();
    const key = [`/api/v1/o/acme/contacts/${ID}/`];
    await waitFor(() =>
      expect(open.client.getQueryState(key)?.error).toMatchObject({ status: 500 }),
    );
    await act(async () => {}); // React ya pintó con el 500
    expect(screen.getByRole("alert")).toHaveTextContent(/No tienes permiso/);
    expect(screen.queryByRole("heading", { name: "Rosa Díaz" })).toBeNull();
    reply = ok(rosa);
    await open.client.refetchQueries();
    expect(await screen.findByRole("heading", { name: "Rosa Díaz" })).toBeVisible();
  });

  it("error con «Reintentar», que sigue montado mientras reintenta y deja el foco en el título", async () => {
    let release = () => {};
    let reply: { status: number; body: unknown } = { status: 500, body: {} };
    const api = mockApi({ [CARD]: () => reply });
    cardOf(viewer);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Algo salió mal de nuestro lado/);
    const fetches = api.mock.calls.length;
    const handle = api.getMockImplementation()!;
    api.mockImplementationOnce(async (input, init) => {
      await new Promise<void>((done) => (release = done));
      return handle(input, init);
    });
    reply = ok(rosa);
    const again = screen.getByRole("button", { name: "Reintentar" });
    again.focus();
    fireEvent.click(again);
    const busy = await screen.findByRole("button", { name: "Cargando el cliente…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(busy); // ocupado: se une a la petición en vuelo
    release();
    const heading = await screen.findByRole("heading", { name: "Rosa Díaz" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(api).toHaveBeenCalledTimes(fetches + 1);
  });

  it("si el usuario se fue a otra parte mientras reintentaba, el foco no se mueve", async () => {
    let release = () => {};
    let reply: { status: number; body: unknown } = { status: 500, body: {} };
    const api = mockApi({ [CARD]: () => reply });
    cardOf(viewer);
    await screen.findByRole("alert");
    const handle = api.getMockImplementation()!;
    api.mockImplementationOnce(async (input, init) => {
      await new Promise<void>((done) => (release = done));
      return handle(input, init);
    });
    reply = ok(rosa);
    const again = screen.getByRole("button", { name: "Reintentar" });
    again.focus();
    fireEvent.click(again);
    const back = screen.getByRole("link", { name: "Clientes" });
    back.focus(); // mientras reintenta
    release();
    await screen.findByRole("heading", { name: "Rosa Díaz" });
    await act(async () => {});
    expect(back).toHaveFocus();
  });

  it("al llegar, el foco va al título; no se lo quita a quien ya está en otra parte", async () => {
    mockApi({ [CARD]: ok(rosa) });
    const view = cardOf(viewer);
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus(); // sin anuncio de Next
    expect(await screen.findByRole("heading", { name: "Rosa Díaz" })).toHaveFocus();
    view.unmount();
    const elsewhere = document.body.appendChild(document.createElement("button"));
    try {
      elsewhere.focus();
      cardOf(viewer);
      await screen.findByRole("heading", { name: "Rosa Díaz" });
      expect(elsewhere).toHaveFocus();
    } finally {
      elsewhere.remove();
    }
  });

  it("sin sesión (401) queda el aviso de carga, sin error: el login lo decide el proveedor", async () => {
    const api = mockApi({ [CARD]: { status: 401, body: { code: "NOT_AUTHENTICATED" } } });
    cardOf(viewer);
    await waitFor(() => expect(api).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Cargando"));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("a 390 px, una columna; lo largo se parte y la pestaña se toca con el dedo", async () => {
    mockApi({ [CARD]: ok({ ...rosa, display_name: "R".repeat(200) }) });
    cardOf(viewer);
    const heading = await screen.findByRole("heading", { level: 1, name: "R".repeat(200) });
    expect(heading).toHaveClass("wrap-anywhere");
    const lists = document.querySelectorAll("dl");
    expect(lists).toHaveLength(2);
    for (const list of lists) {
      expect(list).toHaveClass("grid", "sm:grid-cols-2");
      expect(list).not.toHaveClass("grid-cols-2");
    }
    expect(value("Correo")).toHaveClass("wrap-anywhere");
    expect(screen.getByRole("tab")).toHaveClass("h-11", "lg:h-9");
  });
});
