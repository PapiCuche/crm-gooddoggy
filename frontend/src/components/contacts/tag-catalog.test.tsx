import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/o/acme/clientes/etiquetas" }));

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext, Tag } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { ContactsNav, TagCatalog } from "./tag-catalog";

const LIST = "/api/v1/o/acme/tags/";
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: ["contacts.view", ...codes].map((code) => ({ code, scopes: [] })),
});
const owner = tenant("settings.manage");
const tag = (id: string, name: string, color: string, is_active = true): Tag => ({
  ...{ id, name, color, is_active, applies_to: "CONTACT" },
});

type Answer = { status: number; body?: unknown };
const reply = ({ status, body }: Answer) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status });
// Una API de mentira con estado: la lista, `POST` y `PATCH`. `answerOnce`: lo que responde la
// próxima escritura en su lugar; `hold`: las escrituras esperan hasta que el test las suelta.
function server(initial: Tag[]) {
  let rows = initial;
  let next: Answer | null = null;
  let hold: Promise<void> | null = null;
  const api = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (method === "GET" && url === LIST) return reply({ status: 200, body: { results: rows } });
    if (hold) await hold;
    if (next) {
      const answer = next;
      next = null;
      return reply(answer);
    }
    const data = JSON.parse(String(init?.body));
    if (method === "POST" && url === LIST) {
      const made = tag(`t${rows.length + 1}`, data.name, data.color);
      rows = [...rows, made];
      return reply({ status: 201, body: made });
    }
    const row = rows.find((one) => url === `${LIST}${one.id}/`);
    if (!row || method !== "PATCH") return reply({ status: 404, body: { code: "NOT_FOUND" } });
    const saved = { ...row, ...data };
    rows = rows.map((one) => (one === row ? saved : one));
    return reply({ status: 200, body: saved });
  });
  // Otra persona la cambia: la lista lo trae cuando se vuelva a pedir.
  const rename = (id: string, name: string) =>
    (rows = rows.map((one) => (one.id === id ? { ...one, name } : one)));
  vi.stubGlobal("fetch", api);
  return Object.assign(api, {
    answerOnce: (answer: Answer) => (next = answer),
    rename,
    hold: () => {
      let release = () => {};
      hold = new Promise<void>((done) => (release = done));
      return () => {
        hold = null;
        release();
      };
    },
    sent: () =>
      api.mock.calls.flatMap(([url, init]) =>
        init?.method && init.method !== "GET"
          ? [`${init.method} ${String(url).slice(LIST.length)} ${String(init.body)}`]
          : [],
      ),
  });
}
const screenOf = (who: SelfContext) =>
  renderApp(
    <TenantProvider value={who}>
      <ContactsNav />
      <TagCatalog />
    </TenantProvider>,
  );
const form = () => screen.getByRole("form");
const field = (label: string) => within(form()).getByLabelText(label) as HTMLInputElement;
// Como una pulsación real: el botón recibe el foco antes del clic.
function press(name: string, inside: HTMLElement = document.body) {
  const button = within(inside).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
  return button;
}
const status = () =>
  screen.getAllByRole("status").find((node) => !node.textContent?.includes("en la lista"))!;
const vip = tag("t1", "VIP", "#ffdb5b");
const old = tag("t2", "Antiguo", "#202020", false);

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => vi.unstubAllGlobals());

describe("TagCatalog", () => {
  it("quien ve contactos lee el catálogo, sin escrituras; «Etiquetas» es una pestaña de «Clientes»", async () => {
    server([vip, old]);
    screenOf(tenant());
    const list = await screen.findByRole("list", { name: "Etiquetas" });
    const [first, second] = within(list).getAllByRole("listitem");
    expect(first).toHaveTextContent("VIP#ffdb5b");
    expect(second).toHaveTextContent("Antiguo· desactivada#202020");
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText(/Solo quien administra la configuración/)).toBeVisible();
    const nav = screen.getByRole("navigation", { name: "Secciones de clientes" });
    expect(within(nav).getByRole("link", { name: "Clientes" })).toHaveAttribute(
      "href",
      "/o/acme/clientes",
    );
    expect(within(nav).getByRole("link", { name: "Etiquetas" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("crea con la paleta o un #rrggbb, en minúsculas, una vez por pulsación", async () => {
    const api = server([vip]);
    screenOf(owner);
    await screen.findByText("VIP");
    press("Crear etiqueta");
    expect(field("Nombre")).toHaveFocus();
    expect(field("Color")).toHaveValue("#ffdb5b"); // empieza en miel
    expect(field("Nombre")).toHaveClass("text-[16px]");
    fireEvent.input(field("Nombre"), { target: { value: "Frecuente" } });
    // La paleta escribe en el campo y la muestra la enseña con su texto en blanco.
    const red = within(form()).getByRole("button", { name: "Rojo (#b42318)" });
    expect(red).toHaveClass("size-11", "sm:size-9");
    fireEvent.click(red);
    expect(field("Color")).toHaveValue("#b42318");
    expect(red).toHaveAttribute("aria-pressed", "true");
    // Elegido se marca dentro (el anillo es del foco); el borde contrasta 3:1 como `Choice`.
    expect(red).toHaveTextContent("✓");
    expect(red).toHaveClass("border-foreground/50");
    expect(within(form()).getByRole("button", { name: "Miel (#ffdb5b)" })).toHaveTextContent("");
    const sample = within(form()).getByText("Frecuente").parentElement!;
    expect(sample).toHaveStyle({ backgroundColor: "#b42318", color: "#ffffff" });
    // Uno libre: lo que no es #rrggbb no se envía y se dice junto al campo.
    fireEvent.input(field("Color"), { target: { value: "rojo" } });
    expect(red).toHaveAttribute("aria-pressed", "false");
    press("Crear etiqueta", form());
    expect(field("Color")).toHaveAccessibleDescription(/como #RRGGBB/);
    expect(api.sent()).toEqual([]);
    fireEvent.input(field("Color"), { target: { value: " #1F9D55 " } });
    const release = api.hold();
    const submit = press("Crear etiqueta", form());
    fireEvent.submit(form());
    fireEvent.click(submit);
    release();
    await waitFor(() => expect(status()).toHaveTextContent("Etiqueta «Frecuente» creada."));
    expect(api.sent()).toEqual(['POST  {"name":"Frecuente","color":"#1f9d55"}']);
    expect(await screen.findByText("#1f9d55")).toBeVisible(); // la lista se volvió a pedir
    expect(screen.getByRole("button", { name: "Crear etiqueta" })).toHaveFocus();
  });

  it("`TAG_NAME_TAKEN` se dice junto al nombre, con el foco en él", async () => {
    const api = server([old]);
    screenOf(owner);
    await screen.findByText("Antiguo");
    press("Crear etiqueta");
    fireEvent.input(field("Nombre"), { target: { value: "antiguo" } });
    api.answerOnce({ status: 409, body: { code: "TAG_NAME_TAKEN" } });
    press("Crear etiqueta", form());
    await waitFor(() =>
      expect(field("Nombre")).toHaveAccessibleDescription(/Ya hay una etiqueta con ese nombre/),
    );
    expect(field("Nombre")).toHaveFocus();
  });

  it("editar envía solo lo cambiado; sin cambios no envía; un 404 lo explica la lista", async () => {
    const api = server([vip]);
    screenOf(owner);
    await screen.findByText("VIP");
    press("Editar la etiqueta VIP");
    expect(field("Nombre")).toHaveValue("VIP");
    press("Guardar", form());
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(api.sent()).toEqual([]);
    expect(status()).toHaveTextContent(
      "Sin cambios en la etiqueta «VIP»: no había nada que guardar.",
    );
    press("Editar la etiqueta VIP");
    fireEvent.input(field("Color"), { target: { value: "#FFDB5B" } }); // igual, en mayúsculas
    fireEvent.input(field("Nombre"), { target: { value: "Cliente VIP" } });
    press("Guardar", form());
    await waitFor(() => expect(status()).toHaveTextContent("Etiqueta «Cliente VIP» guardada."));
    expect(api.sent()).toEqual(['PATCH t1/ {"name":"Cliente VIP"}']);
    press("Editar la etiqueta Cliente VIP");
    fireEvent.input(field("Nombre"), { target: { value: "Otra" } });
    api.answerOnce({ status: 404, body: { code: "NOT_FOUND" } });
    press("Guardar", form());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La etiqueta «Cliente VIP» ya no está en esta organización.",
    );
    expect(screen.queryByRole("form")).toBeNull();
  });

  it("compara con lo que había al abrir: no deshace el cambio de otra persona", async () => {
    const api = server([vip]);
    const view = screenOf(owner);
    await screen.findByText("VIP");
    press("Editar la etiqueta VIP");
    api.rename("t1", "Gold"); // otra persona la renombra y la lista se relee
    await act(() => view.client.refetchQueries());
    await screen.findByText("Gold");
    expect(form()).toBeInTheDocument(); // el formulario sigue abierto, con lo que tenía
    fireEvent.input(field("Color"), { target: { value: "#b42318" } });
    press("Guardar", form());
    await waitFor(() => expect(status()).toHaveTextContent("Etiqueta «Gold» guardada."));
    expect(api.sent()).toEqual(['PATCH t1/ {"color":"#b42318"}']);
  });

  it("desactiva y reactiva una vez por pulsación; un 403 se dice en la fila", async () => {
    const api = server([vip]);
    screenOf(owner);
    await screen.findByText("VIP");
    const release = api.hold();
    const held = screen.getByRole("button", { name: "Desactivar la etiqueta VIP" });
    expect(fireEvent.keyDown(held, { key: "Enter", repeat: true })).toBe(false); // no alterna
    expect(api.sent()).toEqual([]);
    const button = press("Desactivar la etiqueta VIP");
    fireEvent.click(button);
    await waitFor(() => expect(button).toHaveTextContent("Guardando…"));
    fireEvent.click(button);
    release();
    await waitFor(() =>
      expect(status()).toHaveTextContent(
        "Etiqueta «VIP» desactivada: sigue en los clientes que la llevan.",
      ),
    );
    expect(api.sent()).toEqual(['PATCH t1/ {"is_active":false}']);
    expect(screen.getByText("· desactivada")).toBeVisible();
    expect(button).toHaveAccessibleName("Reactivar la etiqueta VIP");
    expect(button).toHaveClass("min-h-11");
    api.answerOnce({ status: 403, body: { code: "PERMISSION_DENIED" } });
    press("Reactivar la etiqueta VIP");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para cambiar las etiquetas.",
    );
    press("Reactivar la etiqueta VIP");
    await waitFor(() => expect(status()).toHaveTextContent("Etiqueta «VIP» reactivada."));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
