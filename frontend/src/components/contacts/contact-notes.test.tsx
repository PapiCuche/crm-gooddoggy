import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Contact, Note, SelfContext } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { ContactCard } from "./contact-card";

const ID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const CARD = `/api/v1/o/acme/contacts/${ID}/`;
const NOTES = `${CARD}notes/`;
const MEMBERS = "/api/v1/o/acme/members/?limit=200";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: grants(...codes),
});
const writer = tenant("contacts.view", "contacts.update", "users.view");
const rosa = {
  ...{ id: ID, display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz", version: 1 },
  ...{ created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-23T10:00:00Z", source: "MANUAL" },
} as Contact;
const note = (id: string, changes: Partial<Note> = {}): Note => ({
  ...{ id, contact_id: ID, body: `Nota ${id}`, is_pinned: false, author_user_id: "u1" },
  ...{ updated_by_user_id: null, version: 1 },
  ...{ created_at: "2026-10-23T15:30:00Z", updated_at: "2026-10-23T15:30:00Z" },
  ...changes,
});
const luis = {
  ...{ id: "m-u2", status: "ACTIVE", joined_at: "2026-01-01T00:00:00Z", roles: [] },
  ...{ user: { id: "u2", email: "luis@acme.pe", first_name: "Luis", last_name: "Ramos" } },
  default_branch: null,
};

type Answer = { status: number; body?: unknown };
const reply = ({ status, body }: Answer) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status });
// Una API de mentira: la lista, la más reciente primero, con un cursor si hay más de `per`
// notas. `fail`: la lista responde eso en lugar de las notas. `hold`: la próxima petición a
// esa ruta tarda hasta que el test la suelta.
function server(rows: Note[], per = 50) {
  let failing: Answer | null = null;
  let held: { prefix: string; until: Promise<void> } | null = null;
  const api = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    if (held && url.startsWith(held.prefix)) {
      const { until } = held;
      held = null;
      await until;
    }
    const method = init?.method ?? "GET";
    if (url === CARD) return reply({ status: 200, body: rosa });
    if (url === MEMBERS) return reply({ status: 200, body: { results: [luis], next: null } });
    if (method === "GET" && url.startsWith(NOTES)) {
      if (failing) return reply(failing);
      const from = url.endsWith("?cursor=p2") ? per : 0;
      const next = rows.length > from + per ? "p2" : null;
      return reply({ status: 200, body: { results: rows.slice(from, from + per), next } });
    }
    return reply({ status: 404, body: { code: "NOT_FOUND" } });
  });
  vi.stubGlobal("fetch", api);
  return Object.assign(api, {
    fail: (answer: Answer | null) => (failing = answer),
    hold: (prefix: string) => {
      let release = () => {};
      held = { prefix, until: new Promise<void>((done) => (release = done)) };
      return () => release();
    },
  });
}
const lists = (api: ReturnType<typeof server>) =>
  api.mock.calls.filter(
    ([url, init]) => (init?.method ?? "GET") === "GET" && String(url).startsWith(NOTES),
  ).length;

async function notesOf(who: SelfContext) {
  const view = renderApp(
    <TenantProvider value={who}>
      <ContactCard contactId={ID} />
    </TenantProvider>,
  );
  fireEvent.click(await screen.findByRole("tab", { name: "Notas" }));
  return view;
}
const panel = () => screen.getByRole("tabpanel", { name: "Notas" });
const items = () =>
  within(screen.getByRole("list", { name: "Notas del cliente" })).getAllByRole("listitem");
function press(name: string | RegExp, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus(); // como una pulsación real: el botón recibe el foco antes del clic
  fireEvent.click(button);
}

afterEach(() => vi.unstubAllGlobals());

describe("ContactCard: notas", () => {
  it("«Notas» es la segunda pestaña: flechas, Inicio y Fin; solo con `contacts.view`; se pide al abrirla", async () => {
    const api = server([note("n1")]);
    const view = renderApp(
      <TenantProvider value={writer}>
        <ContactCard contactId={ID} />
      </TenantProvider>,
    );
    const summary = await screen.findByRole("tab", { name: "Resumen" });
    const notes = screen.getByRole("tab", { name: "Notas" });
    expect([summary.tabIndex, notes.tabIndex]).toEqual([0, -1]); // una sola parada de Tab
    expect(screen.queryByRole("tabpanel", { name: "Notas" })).toBeNull(); // oculto
    expect(lists(api)).toBe(0);
    summary.focus();
    expect(fireEvent.keyDown(summary, { key: "ArrowRight" })).toBe(false); // sin desplazar
    expect(notes).toHaveFocus();
    expect(notes).toHaveAttribute("aria-selected", "true");
    expect([summary.tabIndex, notes.tabIndex]).toEqual([-1, 0]);
    expect(notes).toHaveAttribute("aria-controls", panel().id);
    expect(screen.queryByRole("tabpanel", { name: "Resumen" })).toBeNull();
    expect(await within(panel()).findByText("Nota n1")).toBeVisible();
    fireEvent.keyDown(notes, { key: "ArrowRight" }); // da la vuelta
    expect(summary).toHaveFocus();
    fireEvent.keyDown(summary, { key: "ArrowLeft" });
    expect(notes).toHaveFocus();
    fireEvent.keyDown(notes, { key: "Home" });
    expect(summary).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(summary, { key: "End" });
    expect(notes).toHaveAttribute("aria-selected", "true");
    expect(fireEvent.keyDown(notes, { key: "a" })).toBe(true); // otras teclas no se tocan
    fireEvent.click(summary);
    fireEvent.click(notes);
    expect(lists(api)).toBe(1); // sigue montada: no se vuelve a pedir
    view.unmount();
    await waitFor(() => expect(view.client.getQueryCache().getAll()).toEqual([])); // ni en memoria
    renderApp(
      <TenantProvider value={tenant("users.view")}>
        <ContactCard contactId={ID} />
      </TenantProvider>,
    );
    expect(await screen.findAllByRole("tab")).toHaveLength(1);
  });

  it("de la más reciente a la más antigua: autor, fecha de Lima, «Fijada» y «editada por»", async () => {
    const api = server([
      note("n3", { body: "Prefiere WhatsApp\nNo llamar antes de las 9", is_pinned: true }),
      note("n2", { author_user_id: "u2", updated_by_user_id: "u1" }),
      note("n1", { author_user_id: "u9", updated_by_user_id: "u9" }),
      note("n0", { updated_by_user_id: "u2" }),
    ]);
    const release = api.hold(MEMBERS); // el directorio tarda: ni «Otro miembro» mientras
    await notesOf(writer);
    await within(panel()).findByText("Nota n2");
    expect(items()[1]).toHaveTextContent(/^…Cargando · /);
    expect(within(items()[1]!).getByText("…")).toHaveAttribute("aria-hidden");
    expect(items()[3]).not.toHaveTextContent("editada"); // ni «editada por Cargando»
    release();
    await within(panel()).findByText("Luis Ramos");
    const [first, second, third] = items();
    expect(first).toHaveTextContent(/^Tú · 23 oct\. 2026, 10:30 a\. m\. Fijada/);
    expect(first!.querySelector("time")).toHaveAttribute("dateTime", "2026-10-23T15:30:00Z");
    expect(within(first!).getByText(/^Prefiere/)).toHaveClass("whitespace-pre-wrap");
    expect(within(first!).getByText(/^Prefiere/).textContent).toContain("\nNo llamar");
    expect(first).toHaveClass("border-l-accent");
    expect(second).toHaveTextContent(/^Luis Ramos · [^·]* editada por TúNota n2/);
    expect(second).not.toHaveTextContent("Fijada");
    expect(second).not.toHaveClass("border-l-accent");
    expect(third).toHaveTextContent(/^Otro miembro · [^·]*Nota n1/);
    expect(third).not.toHaveTextContent("editada"); // se editó a sí mismo: nada
    expect(items()[3]).toHaveTextContent("editada por Luis Ramos");
  });

  it("sin `users.view`, «Otro miembro» y no se pide el directorio; vacía, lo dice", async () => {
    const api = server([note("n1", { author_user_id: "u2" })]);
    const view = await notesOf(tenant("contacts.view"));
    expect(await within(panel()).findByText("Otro miembro")).toBeVisible();
    expect(api.mock.calls.map(([url]) => String(url))).not.toContain(MEMBERS);
    view.unmount();
    server([]);
    await notesOf(writer);
    expect(await within(panel()).findByText("Este cliente todavía no tiene notas.")).toBeVisible();
  });

  it("un error con «Reintentar», ocupado mientras reintenta; sin sesión (401), sigue cargando", async () => {
    const api = server([note("n1")]);
    api.fail({ status: 500, body: {} }); // tampoco al reintentar sola
    const view = await notesOf(writer);
    expect(await within(panel()).findByRole("alert")).toHaveTextContent(/Algo salió mal/);
    api.fail(null);
    const before = lists(api);
    const release = api.hold(NOTES);
    press("Reintentar", panel());
    const busy = within(panel()).getByRole("button", { name: "Cargando las notas…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(busy); // no cancela la petición en vuelo con otra
    release();
    expect(await within(panel()).findByText("Nota n1")).toBeVisible();
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Notas del cliente" })).toHaveFocus(),
    );
    expect(lists(api)).toBe(before + 1);
    view.unmount();
    api.fail({ status: 401, body: { code: "NOT_AUTHENTICATED" } });
    const again = await notesOf(writer);
    const query = () => again.client.getQueryCache().find({ queryKey: [NOTES, "pages"] });
    await waitFor(() => expect(query()?.state.status).toBe("error"));
    expect(within(panel()).getByRole("status")).toHaveTextContent("Cargando las notas…");
    expect(within(panel()).queryByRole("alert")).toBeNull();
  });

  it("por cursor, con «Cargar más»; sin `contacts.view` (403), se dice; un 404, «no encontrada»", async () => {
    const api = server([note("n3"), note("n2"), note("n1")], 2);
    const view = await notesOf(writer);
    await within(panel()).findByText("Nota n2");
    expect(within(panel()).queryByText("Nota n1")).toBeNull();
    // Abierta, una negativa la cierra (sin «Cargar más»); una respuesta buena la reabre.
    api.fail({ status: 403, body: { code: "PERMISSION_DENIED" } });
    await act(() => view.client.refetchQueries());
    expect(await within(panel()).findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver las notas de este cliente.",
    );
    expect(within(panel()).queryByRole("button")).toBeNull();
    expect(within(panel()).queryByText("Nota n2")).toBeNull();
    api.fail(null);
    await act(() => view.client.refetchQueries());
    await within(panel()).findByText("Nota n2");
    press("Cargar más", panel());
    fireEvent.click(within(panel()).getByRole("button", { name: "Cargando más…" })); // ocupado
    expect(await within(panel()).findByText("Nota n1")).toBeVisible();
    await waitFor(() => expect(items()[2]).toHaveFocus());
    expect(within(panel()).queryByRole("button", { name: "Cargar más" })).toBeNull();
    const urls = api.mock.calls.map(([url]) => String(url));
    expect(urls.filter((url) => url === `${NOTES}?cursor=p2`)).toHaveLength(1);
    view.unmount();
    api.fail({ status: 404, body: { code: "NOT_FOUND" } });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const thrown = new Promise<unknown>((resolve) =>
      window.addEventListener("error", (event) => resolve(event.error), { once: true }),
    );
    await notesOf(writer);
    expect(await thrown).toMatchObject({ message: "NEXT_NOT_FOUND" });
    errors.mockRestore();
    await act(async () => {});
  });

  it("a 390 px: lo largo se parte", async () => {
    server([note("n1", { body: "x".repeat(300) })]);
    await notesOf(writer);
    const li = await within(panel()).findByRole("listitem");
    expect(within(li).getByText("x".repeat(300))).toHaveClass("wrap-anywhere");
  });

  it("el cuerpo es texto: un HTML se ve tal cual, sin elementos", async () => {
    const html = "<b>x</b><img src=x onerror=alert(1)>";
    server([note("n1", { body: html })]);
    await notesOf(writer);
    const li = await within(panel()).findByRole("listitem");
    expect(within(li).getByText(html)).toBeVisible();
    expect(li.querySelector("b, img")).toBeNull();
  });
});
