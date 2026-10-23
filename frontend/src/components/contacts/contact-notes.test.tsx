import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { onlineManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  tags: [] as Contact["tags"],
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
// Una API de mentira con estado, como la real: la lista, la más reciente primero, con un cursor
// si hay más de `per` notas; `POST`, que la pone la primera; `PATCH` con su `version` (409 si ya
// no es la vigente) y `DELETE`; una nota que no está, 404. `fail`: la lista responde eso en lugar
// de las notas. `hold`: la próxima petición a esa ruta tarda hasta que el test la suelta.
function server(initial: Note[], per = 50) {
  let rows = initial;
  let failing: Answer | null = null;
  let held: { prefix: string; until: Promise<void> } | null = null;
  let next: Answer | null = null;
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
    if (method !== "GET" && next) {
      const answer = next;
      next = null;
      return reply(answer);
    }
    const data = JSON.parse(String(init?.body ?? "{}"));
    if (method === "POST" && url === NOTES) {
      const fresh = note(`n${rows.length + 100}`, data);
      rows = [fresh, ...rows];
      return reply({ status: 201, body: fresh });
    }
    const row = rows.find((one) => url === `${NOTES}${one.id}/`);
    if (!row) return reply({ status: 404, body: { code: "NOT_FOUND" } });
    if (method === "DELETE") {
      rows = rows.filter((one) => one !== row);
      return reply({ status: 204 });
    }
    const { version, ...changes } = data;
    if (version !== row.version) {
      return reply({ status: 409, body: { code: "NOTE_VERSION_CONFLICT" } });
    }
    const saved = { ...row, ...changes, version: row.version + 1, updated_by_user_id: "u1" };
    rows = rows.map((one) => (one === row ? saved : one));
    return reply({ status: 200, body: saved });
  });
  vi.stubGlobal("fetch", api);
  return Object.assign(api, {
    fail: (answer: Answer | null) => (failing = answer),
    // Otra persona cambia la nota mientras tanto.
    touch: (id: string, changes: Partial<Note>) =>
      (rows = rows.map((one) =>
        one.id === id ? { ...one, ...changes, version: one.version + 1 } : one,
      )),
    // La próxima escritura responde esto (las lecturas de fondo, como al volver la red, no).
    answerOnce: (answer: Answer) => (next = answer),
    hold: (prefix: string) => {
      let release = () => {};
      held = { prefix, until: new Promise<void>((done) => (release = done)) };
      return () => release();
    },
  });
}
const sent = (api: ReturnType<typeof server>, method: string) =>
  api.mock.calls.flatMap(([url, init]) =>
    init?.method === method
      ? [{ url: String(url), body: init.body ? JSON.parse(String(init.body)) : undefined }]
      : [],
  );
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
const item = (text: string) => items().find((li) => li.textContent!.includes(text))!;
const editor = () => screen.getByRole("form", { name: "Editar la nota" });
const composer = () => screen.getByRole("form", { name: "Nueva nota" });
const write = (form: HTMLElement, value: string) =>
  fireEvent.change(within(form).getByLabelText("Nota"), { target: { value } });
const live = () => within(panel()).getByRole("status", { name: "" });
function press(name: string | RegExp, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus(); // como una pulsación real: el botón recibe el foco antes del clic
  fireEvent.click(button);
}

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
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
    expect(within(first!).getByRole("button", { name: "Editar" })).toHaveAccessibleDescription(
      "Tú 23 oct. 2026, 10:30 a. m. Fijada",
    );
  });

  it("sin `contacts.update` solo se leen; sin `users.view`, «Otro miembro»; vacía, lo dice", async () => {
    const api = server([note("n1", { author_user_id: "u2" }), note("n0")]); // también la propia
    const view = await notesOf(tenant("contacts.view"));
    expect(await within(panel()).findByText("Otro miembro")).toBeVisible();
    expect(screen.queryByRole("form")).toBeNull();
    expect(within(panel()).queryByRole("button")).toBeNull(); // ni editar, fijar ni borrar
    expect(api.mock.calls.map(([url]) => String(url))).not.toContain(MEMBERS);
    view.unmount();
    server([]);
    await notesOf(writer);
    expect(await within(panel()).findByText("Este cliente todavía no tiene notas.")).toBeVisible();
    expect(composer()).toBeVisible();
  });

  it("añadir envía lo escrito sin espacios exteriores, una vez por pulsación, y anuncia", async () => {
    const api = server([note("n1")]);
    renderApp(
      <TenantProvider value={writer}>
        <ContactCard contactId={ID} />
      </TenantProvider>,
    );
    const tab = await screen.findByRole("tab", { name: "Notas" });
    (document.activeElement as HTMLElement).blur(); // Safari no enfoca el botón pulsado
    fireEvent.click(tab);
    await within(panel()).findByText("Nota n1");
    expect(within(composer()).getByLabelText("Nota")).not.toHaveFocus(); // no lo roba al abrir
    expect(within(composer()).getByLabelText("Nota")).toHaveAccessibleDescription(
      "Hasta 5000 caracteres. Se guarda como texto, con sus saltos de línea.",
    );
    write(composer(), "  Llamar el lunes\nPrefiere WhatsApp \n");
    fireEvent.click(within(composer()).getByLabelText("Fijar la nota"));
    let release = api.hold(NOTES); // el `POST`
    press("Añadir nota", composer());
    fireEvent.click(within(composer()).getByLabelText("Fijar la nota")); // solo pinta el formulario
    fireEvent.submit(composer()); // otra vez, enseguida
    const busy = await within(composer()).findByRole("button", { name: "Añadiendo…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    expect(composer()).toHaveAttribute("aria-busy", "true");
    expect(within(composer()).getByLabelText("Nota")).toHaveAttribute("readonly");
    expect(within(composer()).getByLabelText("Fijar la nota")).not.toBeChecked(); // lo de antes
    fireEvent.click(within(composer()).getByLabelText("Fijar la nota")); // ocupado: no cambia
    expect(within(composer()).getByLabelText("Fijar la nota")).not.toBeChecked();
    fireEvent.submit(composer());
    release();
    await waitFor(() => expect(live()).toHaveTextContent("Nota añadida."));
    expect(sent(api, "POST")).toEqual([
      { url: NOTES, body: { body: "Llamar el lunes\nPrefiere WhatsApp", is_pinned: true } },
    ]);
    expect(items()[0]).toHaveTextContent("Fijada");
    expect(within(composer()).getByLabelText("Nota")).toHaveValue(""); // lista para otra
    expect(within(composer()).getByLabelText("Nota")).toHaveFocus();
    expect(within(composer()).getByLabelText("Fijar la nota")).not.toBeChecked();
    // Otra: el aviso anterior se retira al enviar (el mismo texto se vuelve a anunciar), y el
    // foco no se mueve si el usuario ya está en otra parte.
    write(composer(), "Otra");
    release = api.hold(NOTES);
    press("Añadir nota", composer());
    await waitFor(() => expect(live()).toHaveTextContent(/^$/));
    screen.getByRole("tab", { name: "Resumen" }).focus();
    release();
    await waitFor(() => expect(items()).toHaveLength(3));
    expect(screen.getByRole("tab", { name: "Resumen" })).toHaveFocus();
    expect(items()[1]).toHaveTextContent("Fijada");
  });

  it("vacía no se envía; los errores van por `code` sin repetir lo escrito; sin red, se dice", async () => {
    const api = server([]);
    await notesOf(writer);
    await within(panel()).findByText(/todavía no tiene notas/);
    const area = within(composer()).getByLabelText("Nota");
    write(composer(), "  \n ");
    press("Añadir nota", composer());
    expect(area).toHaveAccessibleDescription("Escribe la nota.");
    expect(area).toHaveFocus();
    write(composer(), "secreto\u202e");
    expect(area).toHaveAccessibleDescription(/^Hasta 5000/); // escribir retira el aviso
    api.answerOnce({ status: 400, body: { code: "VALIDATION_ERROR", fields: { body: ["x"] } } });
    press("Añadir nota", composer());
    await waitFor(() =>
      expect(area).toHaveAccessibleDescription(
        "La nota no es válida: solo texto, de 1 a 5000 caracteres, sin caracteres de control.",
      ),
    );
    expect(within(composer()).getByRole("alert").textContent).not.toContain("secreto");
    api.answerOnce({ status: 400, body: { code: "VALIDATION_ERROR", fields: { other: ["x"] } } });
    press("Añadir nota", composer()); // un 400 sin campo conocido es un fallo nuestro
    expect(await within(composer()).findByText(/Algo salió mal de nuestro lado/)).toBeVisible();
    api.answerOnce({ status: 403, body: { code: "PERMISSION_DENIED" } });
    press("Añadir nota", composer());
    await waitFor(() =>
      expect(within(composer()).getByRole("alert")).toHaveTextContent(
        "No tienes permiso para escribir notas de este cliente.",
      ),
    );
    write(composer(), " ");
    press("Añadir nota", composer()); // vacía: solo su aviso, ni un instante el de antes
    expect(composer()).not.toHaveTextContent("No tienes permiso");
    write(composer(), "secreto\u202e");
    api.answerOnce({ status: 403, body: { code: "PERMISSION_DENIED" } });
    press("Añadir nota", composer());
    await within(composer()).findByText(/No tienes permiso/);
    const before = lists(api);
    api.answerOnce({ status: 404, body: { code: "NOT_FOUND" } });
    press("Añadir nota", composer());
    write(composer(), "secreto\u202e más"); // escribir mientras se envía no pierde la respuesta
    expect(await within(composer()).findByRole("alert")).toHaveTextContent(
      "No puedes añadir notas a este cliente: quedó fuera de tu alcance o se borró.",
    );
    await waitFor(() => expect(lists(api)).toBe(before + 1)); // si ya no está, «no encontrada»
    write(composer(), "otra");
    await waitFor(() => expect(within(composer()).queryByRole("alert")).toBeNull()); // escribir lo retira
    onlineManager.setOnline(false);
    try {
      api.mockRejectedValueOnce(new TypeError("sin red"));
      press("Añadir nota", composer());
      expect(await within(composer()).findByRole("alert")).toHaveTextContent("No hay conexión");
      expect(within(composer()).getByRole("button", { name: "Añadir nota" })).toHaveAttribute(
        "aria-disabled",
        "false",
      );
    } finally {
      onlineManager.setOnline(true);
    }
    api.answerOnce({ status: 401, body: { code: "NOT_AUTHENTICATED" } });
    press("Añadir nota", composer()); // sin sesión: el login lo decide el proveedor
    await waitFor(() => expect(sent(api, "POST")).toHaveLength(7));
    await act(async () => {});
    expect(within(composer()).getByRole("button", { name: "Añadiendo…" })).toBeVisible();
    expect(within(composer()).queryByRole("alert")).toBeNull();
    fireEvent.submit(composer());
    expect(sent(api, "POST")).toHaveLength(7);
  });

  it("editar envía solo lo cambiado con la versión; sin cambios no envía; el foco vuelve", async () => {
    const api = server([note("n1", { version: 4, is_pinned: true })]);
    await notesOf(writer);
    press("Editar", await within(panel()).findByRole("listitem"));
    const area = within(editor()).getByLabelText("Nota");
    expect(area).toHaveValue("Nota n1");
    expect(area).toHaveFocus();
    expect(within(editor()).getByLabelText("Fijar la nota")).toBeChecked();
    press("Guardar", editor()); // sin cambios: cierra
    expect(screen.queryByRole("form", { name: "Editar la nota" })).toBeNull();
    expect(within(item("Nota n1")).getByRole("button", { name: "Editar" })).toHaveFocus();
    const trigger = within(item("Nota n1")).getByRole("button", { name: "Editar" });
    expect(fireEvent.keyDown(trigger, { key: "Enter", repeat: true })).toBe(false);
    press("Editar", item("Nota n1"));
    const typing = within(editor()).getByLabelText("Nota");
    expect(fireEvent.keyDown(typing, { key: "Enter", repeat: true })).toBe(true); // saltos de línea
    api.answerOnce({ status: 403, body: { code: "PERMISSION_DENIED" } });
    write(editor(), "x"); // con un cambio, para que se envíe
    press("Guardar", editor());
    expect(await within(editor()).findByRole("alert")).toHaveTextContent(/No tienes permiso/);
    press("Cancelar", editor());
    press("Editar", item("Nota n1")); // abrir de nuevo retira el error de antes
    expect(within(editor()).queryByRole("alert")).toBeNull();
    write(editor(), " Nota n1 corregida ");
    const release = api.hold(NOTES);
    press("Guardar", editor());
    fireEvent.submit(editor());
    fireEvent.click(within(editor()).getByRole("button", { name: "Cancelar" })); // ocupado: sigue
    expect(editor()).toBeVisible();
    release();
    await waitFor(() => expect(live()).toHaveTextContent("Nota guardada."));
    expect(sent(api, "PATCH").slice(1)).toEqual([
      { url: `${NOTES}n1/`, body: { body: "Nota n1 corregida", version: 4 } },
    ]);
    expect(item("Nota n1 corregida")).toBeVisible();
    expect(within(item("corregida")).getByRole("button", { name: "Editar" })).toHaveFocus();
    press("Editar", item("corregida"));
    fireEvent.click(within(editor()).getByLabelText("Fijar la nota"));
    api.fail({ status: 500, body: {} }); // guardada, pero la lista no vuelve: entra igual
    press("Guardar", editor());
    await waitFor(() => expect(live()).toHaveTextContent(/^Nota guardada, pero las notas no/));
    expect(sent(api, "PATCH")[2]!.body).toEqual({ is_pinned: false, version: 5 });
    expect(item("corregida")).not.toHaveTextContent("Fijada");
    press("Fijar", item("corregida")); // con la versión guardada: no es un 409 por lo propio
    await waitFor(() => expect(live()).toHaveTextContent(/^Nota fijada, pero/));
    expect(sent(api, "PATCH")[3]!.body).toEqual({ is_pinned: true, version: 6 });
    api.fail(null);
    press("Editar", await within(panel()).findByRole("listitem"));
    press("Cancelar", editor());
    expect(within(item("corregida")).getByRole("button", { name: "Editar" })).toHaveFocus();
  });

  it("409: el formulario se recarga con lo nuevo y lo dice; repetir guarda con la versión nueva", async () => {
    const api = server([note("n1")]);
    const view = await notesOf(writer);
    press("Editar", await within(panel()).findByRole("listitem"));
    write(editor(), "Mi cambio");
    api.touch("n1", { body: "Lo que guardó Luis", updated_by_user_id: "u2" });
    press("Guardar", editor());
    // El formulario se monta de nuevo con lo recargado: se busca otra vez.
    await waitFor(() =>
      expect(within(editor()).getByRole("alert")).toHaveTextContent(
        "Otra persona cambió esta nota mientras la editabas. Ahora ves lo que guardó",
      ),
    );
    expect(within(editor()).getByLabelText("Nota")).toHaveValue("Lo que guardó Luis");
    expect(item("Luis")).toHaveTextContent("editada por Luis Ramos");
    write(editor(), "Mi cambio, otra vez");
    press("Guardar", editor());
    await waitFor(() => expect(live()).toHaveTextContent("Nota guardada."));
    expect(sent(api, "PATCH").map(({ body }) => body.version)).toEqual([1, 2]);
    // Sin la lista nueva, lo dice y no recarga.
    press("Editar", item("otra vez"));
    write(editor(), "Tercero");
    api.touch("n1", { body: "Otro más" });
    api.fail({ status: 500, body: {} }); // la lista no vuelve, tampoco al reintentar sola
    press("Guardar", editor());
    await waitFor(() =>
      expect(within(editor()).getByRole("alert")).toHaveTextContent("no se pudo cargar lo nuevo"),
    );
    expect(within(editor()).getByLabelText("Nota")).toHaveValue("Tercero");
    api.fail(null); // al repetir, otro 409 (nunca pisa) y ahora sí se recarga
    press("Guardar", editor());
    await waitFor(() =>
      expect(within(editor()).getByRole("alert")).toHaveTextContent(/Ahora ves lo que guardó/),
    );
    // Lo que se compara es lo que había al abrir, aunque la lista se relea debajo.
    write(editor(), "Cuarto");
    api.touch("n1", { body: "Quinto", is_pinned: true });
    await act(() => view.client.refetchQueries());
    await within(items()[0]!).findByText("Fijada"); // la nota de la lista ya es la nueva
    press("Guardar", editor());
    await waitFor(() => expect(within(editor()).getByLabelText("Nota")).toHaveValue("Quinto"));
    expect(sent(api, "PATCH").at(-1)!.body.version).toBe(4);
  });

  it("solo se ofrece cambiar las propias; las de otros, con `contacts.update` ORGANIZATION", async () => {
    const scoped = (scope: string): SelfContext =>
      ({
        ...writer,
        permissions: [...grants("contacts.view"), { code: "contacts.update", scopes: [scope] }],
      }) as SelfContext;
    server([note("n2", { author_user_id: "u2" }), note("n1")]);
    const view = await notesOf(scoped("OWN")); // el Vendedor de la plantilla
    await within(panel()).findByText("Nota n2");
    expect(within(item("Nota n2")).queryByRole("button")).toBeNull();
    expect(
      within(item("Nota n1"))
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["Editar", "Fijar", "Borrar"]);
    expect(composer()).toBeVisible();
    view.unmount();
    await notesOf(scoped("ORGANIZATION"));
    await within(panel()).findByText("Nota n2");
    expect(within(item("Nota n2")).getAllByRole("button")).toHaveLength(3);
    expect(within(item("Nota n1")).getAllByRole("button")).toHaveLength(3);
  });

  it("un 404 al editar (se borró, o ya no se puede cambiar) cierra, lo explica y relee", async () => {
    const api = server([note("n1")]);
    await notesOf(writer);
    press("Editar", await within(panel()).findByRole("listitem"));
    write(editor(), "No es mía");
    api.answerOnce({ status: 404, body: { code: "NOT_FOUND" } });
    const before = lists(api);
    press("Guardar", editor());
    expect(await within(panel()).findByRole("alert")).toHaveTextContent(
      "Esa nota se borró o ya no la puedes cambiar. Las notas se volvieron a cargar.",
    );
    expect(screen.queryByRole("form", { name: "Editar la nota" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Notas del cliente" })).toHaveFocus();
    await waitFor(() => expect(lists(api)).toBe(before + 1));
    press("Editar", item("Nota n1")); // abrir otra cosa retira el aviso
    expect(within(panel()).queryByText(/Esa nota se borró/)).toBeNull();
  });

  it("fijar y quitar de fijadas, una vez por pulsación; un 409 lo explica la lista", async () => {
    const api = server([note("n1")]);
    await notesOf(writer);
    const li = await within(panel()).findByRole("listitem");
    const release = api.hold(NOTES);
    press("Fijar", li);
    fireEvent.click(within(li).getByRole("button", { name: "Fijar" }));
    expect(await within(li).findByRole("button", { name: "Guardando…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    fireEvent.click(within(li).getByRole("button", { name: "Guardando…" }));
    release();
    await waitFor(() => expect(live()).toHaveTextContent("Nota fijada."));
    expect(sent(api, "PATCH")).toEqual([
      { url: `${NOTES}n1/`, body: { is_pinned: true, version: 1 } },
    ]);
    expect(item("Nota n1")).toHaveTextContent("Fijada");
    press("Quitar de fijadas", item("Nota n1"));
    await waitFor(() => expect(live()).toHaveTextContent("Nota quitada de fijadas."));
    expect(sent(api, "PATCH")[1]!.body).toEqual({ is_pinned: false, version: 2 });
    api.touch("n1", {});
    api.answerOnce({ status: 409, body: { code: "NOTE_VERSION_CONFLICT" } });
    press("Fijar", item("Nota n1"));
    expect(await within(panel()).findByRole("alert")).toHaveTextContent(
      "Otra persona cambió esa nota: las notas se volvieron a cargar.",
    );
    api.answerOnce({ status: 403, body: { code: "PERMISSION_DENIED" } });
    press("Fijar", item("Nota n1"));
    expect(await within(item("Nota n1")).findByRole("alert")).toHaveTextContent(
      "No tienes permiso para escribir notas de este cliente.",
    );
    api.answerOnce({ status: 404, body: { code: "NOT_FOUND" } });
    press("Fijar", item("Nota n1"));
    expect(await within(panel()).findByText(/Esa nota se borró/)).toHaveAttribute("role", "alert");
    api.answerOnce({ status: 401, body: { code: "NOT_AUTHENTICATED" } });
    press("Fijar", item("Nota n1")); // sin sesión: ocupado, sin error; el login, al proveedor
    expect(
      await within(item("Nota n1")).findByRole("button", { name: "Guardando…" }),
    ).toBeVisible();
    await act(async () => {});
    expect(within(item("Nota n1")).getByRole("button", { name: "Guardando…" })).toBeVisible();
    expect(within(item("Nota n1")).queryByRole("alert")).toBeNull();
  });

  it("borrar pide confirmación con el foco en «Cancelar»; borra una vez y el foco va al título", async () => {
    const api = server([note("n2"), note("n1")]);
    await notesOf(writer);
    await within(panel()).findByText("Nota n2");
    press("Borrar", item("Nota n2"));
    const ask = within(item("Nota n2")).getByRole("group", { name: "Borrar la nota" });
    expect(ask).toHaveAccessibleDescription("¿Borrar esta nota? Deja de verse en la ficha.");
    expect(within(ask).getByRole("button", { name: "Cancelar" })).toHaveFocus();
    press("Cancelar", ask);
    expect(within(item("Nota n2")).getByRole("button", { name: "Borrar" })).toHaveFocus();
    press("Borrar", item("Nota n2"));
    api.answerOnce({ status: 403, body: { code: "PERMISSION_DENIED" } });
    press("Borrar", within(item("Nota n2")).getByRole("group"));
    expect(await within(item("Nota n2")).findByRole("alert")).toHaveTextContent(
      "No tienes permiso para escribir notas de este cliente.",
    );
    press("Cancelar", within(item("Nota n2")).getByRole("group"));
    press("Borrar", item("Nota n2")); // abrir de nuevo retira el error de antes
    expect(within(item("Nota n2")).queryByRole("alert")).toBeNull();
    const release = api.hold(NOTES);
    press("Borrar", within(item("Nota n2")).getByRole("group"));
    const busy = await within(item("Nota n2")).findByRole("button", { name: "Borrando…" });
    fireEvent.click(busy);
    fireEvent.click(within(item("Nota n2")).getByRole("button", { name: "Cancelar" }));
    expect(within(item("Nota n2")).getByRole("group")).toBeVisible(); // ocupado: sigue
    release();
    await waitFor(() => expect(live()).toHaveTextContent("Nota borrada."));
    expect(sent(api, "DELETE").map(({ url }) => url)).toEqual([
      `${NOTES}n2/`,
      `${NOTES}n2/`, // el 403 y el bueno: ninguno repetido
    ]);
    expect(items()).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Notas del cliente" })).toHaveFocus();
    api.answerOnce({ status: 404, body: { code: "NOT_FOUND" } });
    press("Borrar", item("Nota n1"));
    press("Borrar", within(item("Nota n1")).getByRole("group"));
    expect(await within(panel()).findByRole("alert")).toHaveTextContent(/Esa nota se borró/);
    expect(within(item("Nota n1")).queryByRole("group")).toBeNull(); // la confirmación se cierra
    api.fail({ status: 500, body: {} }); // borrada, pero la lista no vuelve: sale igual
    press("Borrar", item("Nota n1"));
    press("Borrar", within(item("Nota n1")).getByRole("group"));
    await waitFor(() => expect(live()).toHaveTextContent(/^Nota borrada, pero las notas no/));
    expect(within(panel()).queryByText("Nota n1")).toBeNull();
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
    // Añadida sin poder releer la lista: la nota entra igual, la primera, y se dice.
    write(composer(), "Sin lista");
    api.fail({ status: 500, body: {} });
    press("Añadir nota", composer());
    await waitFor(() =>
      expect(live()).toHaveTextContent(
        "Nota añadida, pero las notas no se pudieron actualizar. Recarga la página.",
      ),
    );
    expect(items().map((li) => li.textContent)).toEqual([
      expect.stringContaining("Sin lista"),
      expect.stringContaining("Nota n1"),
    ]);
    view.unmount();
    api.fail({ status: 401, body: { code: "NOT_AUTHENTICATED" } });
    const again = await notesOf(writer);
    const query = () => again.client.getQueryCache().find({ queryKey: [NOTES, "pages"] });
    await waitFor(() => expect(query()?.state.status).toBe("error"));
    expect(within(panel()).getByText("Cargando las notas…")).toHaveAttribute("role", "status");
    expect(within(panel()).queryByRole("alert")).toBeNull();
  });

  it("añadida con la lista sin cargar y sin poder releerla: una sola vez, vacía y lo dice", async () => {
    const api = server([]);
    api.fail({ status: 500, body: {} });
    await notesOf(writer);
    expect(await within(panel()).findByRole("alert")).toHaveTextContent(/Algo salió mal/);
    write(composer(), "Sin lista nunca");
    press("Añadir nota", composer());
    await waitFor(() =>
      expect(live()).toHaveTextContent(
        "Nota añadida, pero las notas no se pudieron actualizar. Recarga la página.",
      ),
    );
    expect(sent(api, "POST")).toHaveLength(1);
    expect(within(composer()).getByLabelText("Nota")).toHaveValue("");
    expect(within(composer()).queryByRole("alert")).toBeNull();
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

  it("a 390 px: los controles se tocan con el dedo y lo largo se parte", async () => {
    server([note("n1", { body: "x".repeat(300) })]);
    await notesOf(writer);
    const li = await within(panel()).findByRole("listitem");
    expect(within(li).getByText("x".repeat(300))).toHaveClass("wrap-anywhere");
    for (const button of within(li).getAllByRole("button")) {
      expect(button).toHaveClass("min-h-11", "sm:min-h-9");
    }
    expect(within(composer()).getByRole("button")).toHaveClass("min-h-11", "sm:min-h-9");
    expect(within(composer()).getByText("Fijar la nota")).toHaveClass("min-h-11");
    expect(within(composer()).getByLabelText("Nota")).toHaveClass("text-[16px]");
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
