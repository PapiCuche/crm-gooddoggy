import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ notFound: () => {} }));

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Contact, SelfContext, Tag } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { ContactCard } from "./contact-card";

const ID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const CARD = `/api/v1/o/acme/contacts/${ID}/`;
const TAGS = "/api/v1/o/acme/tags/?limit=200";
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: ["contacts.view", ...codes].map((code) => ({ code, scopes: [] })),
});
const tag = (id: string, name: string, is_active = true): Tag => ({
  ...{ id, name, color: "#1a7f37", is_active, applies_to: "CONTACT" },
});
const vip = tag("t1", "VIP");
const frio = tag("t2", "Frío");
const old = tag("t3", "Antiguo", false);
const rosa = (tags: Tag[]): Contact => ({
  ...{ id: ID, display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz" },
  ...{ primary_phone_e164: null, primary_email: null, document_type: null },
  ...{ document_number: null, owner_user_id: "u1", branch_id: null, source: "MANUAL" },
  ...{ created_by_user_id: "u1", created_at: "2026-10-23T10:00:00Z" },
  ...{ updated_at: "2026-10-23T10:00:00Z", version: 1 },
  tags: tags.map(({ id, name, color, is_active }) => ({ id, name, color, is_active })),
});

type Answer = { status: number; body?: unknown };
const reply = ({ status, body }: Answer) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status });
// Una API de mentira con estado: la ficha con sus etiquetas, el catálogo y `PUT`/`DELETE`
// que responden 204 sin cuerpo. `next`: lo que responde la próxima escritura en su lugar.
function server(worn: Tag[], catalog: Tag[] = [vip, frio, old]) {
  let tags = worn;
  let next: Answer | null = null;
  let hold: Promise<void> | null = null;
  let broken = false;
  let breakCard = false; // la ficha deja de volver tras la próxima escritura
  const api = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    if (url === CARD) return reply(broken ? { status: 500 } : { status: 200, body: rosa(tags) });
    if (url === TAGS) return reply({ status: 200, body: { results: catalog, next: null } });
    const tagId = url.slice(`${CARD}tags/`.length, -1);
    const found = catalog.find((one) => one.id === tagId);
    if (!url.startsWith(`${CARD}tags/`) || !found) return reply({ status: 404 });
    if (hold) await hold;
    if (next) {
      const answer = next;
      next = null;
      return reply(answer);
    }
    if (method === "PUT" && !tags.includes(found)) tags = [...tags, found];
    if (method === "DELETE") tags = tags.filter((one) => one !== found);
    broken = breakCard;
    return reply({ status: 204 });
  });
  vi.stubGlobal("fetch", api);
  return Object.assign(api, {
    answerOnce: (answer: Answer) => (next = answer),
    breakCardAfterWrite: () => (breakCard = true),
    deactivate: (id: string) =>
      (catalog = catalog.map((one) => (one.id === id ? { ...one, is_active: false } : one))),
    // Las escrituras esperan hasta que el test las suelta.
    hold: () => {
      let release = () => {};
      hold = new Promise<void>((done) => (release = done));
      return () => {
        hold = null;
        release();
      };
    },
    writes: () =>
      api.mock.calls.flatMap(([url, init]) =>
        init?.method && init.method !== "GET"
          ? [`${init.method} ${String(url).slice(CARD.length)}`]
          : [],
      ),
    reads: (url: string) => api.mock.calls.filter(([one]) => String(one) === url).length,
  });
}
const cardOf = (who: SelfContext) =>
  renderApp(
    <TenantProvider value={who}>
      <ContactCard contactId={ID} />
    </TenantProvider>,
  );
const box = () => screen.getByRole("region", { name: "Etiquetas" });
const select = () => within(box()).getByRole("combobox", { name: "Añadir etiqueta" });
const options = () => [...(select() as HTMLSelectElement).options].map((one) => one.text);
// Como una pulsación real: el botón recibe el foco antes del clic.
function press(name: string) {
  const button = within(box()).getByRole("button", { name });
  button.focus();
  fireEvent.click(button);
}
const editor = tenant("contacts.update");

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => vi.unstubAllGlobals());

describe("ContactCard: poner y quitar etiquetas", () => {
  it("sin `contacts.update` solo se ven: ni catálogo, ni añadir, ni quitar", async () => {
    const api = server([vip]);
    cardOf(tenant());
    await screen.findByRole("region", { name: "Etiquetas" });
    expect(within(box()).getByText("VIP")).toBeVisible();
    expect(within(box()).queryByRole("button")).toBeNull();
    expect(within(box()).queryByRole("form")).toBeNull();
    expect(api.reads(TAGS)).toBe(0);
  });

  it("pone una etiqueta una vez por pulsación; sigue ocupado hasta que vuelve la ficha", async () => {
    const api = server([vip]);
    cardOf(editor);
    await screen.findByRole("region", { name: "Etiquetas" });
    // Solo las activas que no lleva: ni «VIP» (ya está) ni «Antiguo» (desactivada).
    await waitFor(() => expect(options()).toEqual(["Elige una etiqueta", "Frío"]));
    expect(select()).toHaveClass("text-[16px]", "h-11");
    fireEvent.change(select(), { target: { value: "t2" } });
    const release = api.hold();
    const button = within(box()).getByRole("button", { name: "Añadir" });
    press("Añadir");
    fireEvent.submit(within(box()).getByRole("form", { name: "Añadir etiqueta" })); // otra vez
    fireEvent.click(button);
    expect(await within(box()).findByRole("button", { name: "Añadiendo…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(api.writes()).toEqual(["PUT tags/t2/"]);
    release();
    // 204 sin cuerpo: la ficha se vuelve a pedir y la etiqueta llega con ella.
    await waitFor(() =>
      expect(within(box()).getByRole("status")).toHaveTextContent("Etiqueta «Frío» puesta."),
    );
    expect(within(box()).getByRole("list", { name: "Etiquetas" })).toHaveTextContent("Frío");
    expect(api.reads(CARD)).toBe(2);
    expect(options()).toEqual(["Elige una etiqueta"]);
    expect(select()).toHaveAccessibleDescription("No quedan etiquetas activas por poner.");
    expect(api.writes()).toEqual(["PUT tags/t2/"]);
  });

  it("si la ficha no vuelve tras poner, lo dice", async () => {
    const api = server([]);
    cardOf(editor);
    await screen.findByRole("region", { name: "Etiquetas" });
    await waitFor(() => expect(options()).toContain("VIP"));
    fireEvent.change(select(), { target: { value: "t1" } });
    api.breakCardAfterWrite();
    press("Añadir");
    await waitFor(() =>
      expect(within(box()).getByRole("status")).toHaveTextContent(
        "Hecho, pero la ficha no se pudo actualizar. Recarga la página.",
      ),
    );
  });

  it("sin elegir no se envía y lo dice junto al control", async () => {
    const api = server([]);
    cardOf(editor);
    await screen.findByRole("region", { name: "Etiquetas" });
    expect(within(box()).getByText("Sin etiquetas.")).toBeVisible();
    await waitFor(() => expect(options()).toHaveLength(3));
    press("Añadir");
    expect(select()).toHaveAccessibleDescription("Elige la etiqueta que quieres poner.");
    expect(api.writes()).toEqual([]);
    fireEvent.change(select(), { target: { value: "t1" } });
    expect(select()).not.toHaveAccessibleDescription(/Elige la etiqueta/);
  });

  it("quita una etiqueta una vez por pulsación; el foco va al título del grupo", async () => {
    const api = server([vip, old]);
    cardOf(editor);
    await screen.findByRole("region", { name: "Etiquetas" });
    // También la desactivada se quita. 390 px: 44 px de alto para el dedo.
    const quit = within(box()).getByRole("button", { name: "Quitar la etiqueta «Antiguo»" });
    expect(quit).toHaveClass("min-h-11", "sm:min-h-9");
    const release = api.hold();
    press("Quitar la etiqueta «VIP»");
    press("Quitar la etiqueta «VIP»");
    fireEvent.click(quit); // otra, mientras tanto: tampoco (el foco sigue en «VIP»)
    await waitFor(() =>
      expect(
        within(box()).getByRole("button", { name: "Quitar la etiqueta «VIP»" }),
      ).toHaveTextContent("Quitando…"),
    );
    release();
    await waitFor(() =>
      expect(within(box()).getByRole("status")).toHaveTextContent("Etiqueta «VIP» quitada."),
    );
    expect(api.writes()).toEqual(["DELETE tags/t1/"]);
    const worn = within(box()).getByRole("list", { name: "Etiquetas" });
    expect(worn).not.toHaveTextContent("VIP");
    expect(options()).toContain("VIP"); // vuelve a ofrecerse
    expect(within(box()).getByRole("heading", { name: "Etiquetas" })).toHaveFocus();
    expect(api.reads(CARD)).toBe(2);
  });

  it("`TAG_INACTIVE`: lo dice con su nombre y relee el catálogo, que ya no la ofrece", async () => {
    const api = server([]);
    cardOf(editor);
    await screen.findByRole("region", { name: "Etiquetas" });
    await waitFor(() => expect(options()).toContain("VIP"));
    fireEvent.change(select(), { target: { value: "t1" } });
    api.answerOnce({ status: 409, body: { code: "TAG_INACTIVE" } });
    api.deactivate("t1"); // otra persona la desactivó
    press("Añadir");
    expect(await within(box()).findByRole("alert")).toHaveTextContent(
      "La etiqueta «VIP» está desactivada: ya no se pone en más clientes.",
    );
    await waitFor(() => expect(options()).toEqual(["Elige una etiqueta", "Frío"]));
    expect(api.reads(TAGS)).toBe(2);
    expect(api.reads(CARD)).toBe(1);
  });

  it("un 404 (fuera del alcance de edición o borrado) lo explica y relee la ficha", async () => {
    const api = server([vip]);
    cardOf(editor);
    await screen.findByRole("region", { name: "Etiquetas" });
    api.answerOnce({ status: 404, body: { code: "NOT_FOUND" } });
    press("Quitar la etiqueta «VIP»");
    expect(await within(box()).findByRole("alert")).toHaveTextContent(
      /Ya no puedes cambiar las etiquetas de este cliente/,
    );
    await waitFor(() => expect(api.reads(CARD)).toBe(2));
  });

  it("Enter mantenido no repite; sin sesión (401) sigue ocupado", async () => {
    const api = server([vip]);
    cardOf(editor);
    await screen.findByRole("region", { name: "Etiquetas" });
    await waitFor(() => expect(options()).toContain("Frío"));
    const add = within(box()).getByRole("button", { name: "Añadir" });
    const quit = within(box()).getByRole("button", { name: "Quitar la etiqueta «VIP»" });
    expect(fireEvent.keyDown(add, { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(quit, { key: "Enter", repeat: true })).toBe(false);
    expect(fireEvent.keyDown(quit, { key: "Enter" })).toBe(true); // la primera sí cuenta
    api.answerOnce({ status: 401, body: { code: "NOT_AUTHENTICATED" } });
    press("Quitar la etiqueta «VIP»");
    await waitFor(() => expect(api.writes()).toEqual(["DELETE tags/t1/"]));
    await waitFor(() => expect(quit).toHaveAttribute("aria-disabled", "true"));
    expect(within(box()).queryByRole("alert")).toBeNull();
    press("Quitar la etiqueta «VIP»");
    expect(api.writes()).toEqual(["DELETE tags/t1/"]);
  });

  it("un 403 y otros fallos se dicen por `code`; escribir de nuevo retira el aviso", async () => {
    const api = server([vip]);
    cardOf(editor);
    await screen.findByRole("region", { name: "Etiquetas" });
    api.answerOnce({ status: 403, body: { code: "PERMISSION_DENIED" } });
    press("Quitar la etiqueta «VIP»");
    expect(await within(box()).findByRole("alert")).toHaveTextContent(
      "No tienes permiso para cambiar las etiquetas de este cliente.",
    );
    api.answerOnce({ status: 500, body: {} });
    press("Quitar la etiqueta «VIP»");
    await waitFor(() =>
      expect(within(box()).getByRole("alert")).toHaveTextContent(/Algo salió mal/),
    );
    press("Quitar la etiqueta «VIP»");
    await waitFor(() => expect(within(box()).getByRole("status")).toHaveTextContent("quitada"));
    expect(within(box()).queryByRole("alert")).toBeNull();
    expect(api.reads(CARD)).toBe(2); // solo tras el 204
  });
});
