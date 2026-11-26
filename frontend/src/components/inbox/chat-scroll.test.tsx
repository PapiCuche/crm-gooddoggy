import { act, configure, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Message, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

// UX-INBOX-06b. Sin maquetación en jsdom: 300 px visibles, 100 por fila (300 si dice «largo»).
configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const BASE = "/api/v1/o/acme/conversations";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  ...{ membership_id: "m1", mfa: { required: false, missing: false }, roles: [] },
  permissions: ["conversations.view", "messages.send"].map((code) => ({
    ...{ code, scopes: ["ORGANIZATION"] },
  })),
};
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, version: 1 },
  channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: "2026-11-04T15:30:00Z" },
  ...{ last_message_preview: "Hola", last_message_direction: "INBOUND" },
} as Conversation;
const message = (id: string, body: string, mine = false) =>
  ({
    ...{ id, direction: mine ? "OUTBOUND" : "INBOUND", sender_type: mine ? "USER" : "CONTACT" },
    ...{ sender_user_id: mine ? "u1" : null, message_type: "TEXT", body, client_message_id: null },
    ...{ delivery_status: mine ? "SENT" : null, created_at: "2026-11-04T15:30:00Z" },
    ...{ sent_at: null, delivered_at: null, read_at: null, failed_at: null, dispatch_attempts: 0 },
  }) as Message;

// Alto simulado; asignar `scrollTop` desplaza (con tope) y avisa con `scroll`, como un navegador.
const SEEN = 300;
const tops = new WeakMap<Element, number>();
const isChat = (el: Element) => el.getAttribute("aria-label") === "Mensajes de la conversación";
const grown = new Map<string, number>(); // px de más en la fila con ese texto (aviso, adjunto, estado)
const tall = (li: Element) =>
  (li.textContent?.includes("largo") ? 300 : 100) +
  [...grown].reduce((sum, [text, px]) => sum + (li.textContent?.includes(text) ? px : 0), 0);
// `ResizeObserver` falso: `resized()` avisa al que vigila el contenido de la región.
const observers: { fire: () => void; targets: Element[]; off: boolean }[] = [];
class Observer {
  entry: (typeof observers)[number];
  constructor(fire: () => void) {
    observers.push((this.entry = { fire, targets: [], off: false }));
  }
  observe(target: Element) {
    this.entry.targets.push(target);
  }
  unobserve() {}
  disconnect() {
    this.entry.off = true;
  }
}
const watching = () =>
  observers.filter(({ targets }) => targets.includes(chat().firstElementChild!));
const resized = () => watching().forEach(({ fire, off }) => off || fire());
const rowsOf = (el: Element) => [...el.querySelectorAll("li")];
const full = (el: Element) => rowsOf(el).reduce((sum, li) => sum + tall(li), 0);
const from: number[] = []; // dónde estaba la vista al empezar cada desplazamiento suave
let glide = () => {}; // el suave va a la mitad y termina con `glide()`, hacia el destino del clic
let boxTop = 40;
let hidden = false; // `display: none`: todo mide 0 y `scrollTop` no se escribe
const scrollTo = vi.fn(function (this: HTMLElement, { top, behavior }: ScrollToOptions) {
  from.push(this.scrollTop);
  glide = () => void (this.scrollTop = top!);
  if (behavior === "smooth") this.scrollTop += (top! - SEEN - this.scrollTop) / 2;
  else glide();
});
beforeEach(() => {
  const proto = HTMLElement.prototype;
  vi.spyOn(proto, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return isChat(this) && !hidden ? full(this) : 0;
  });
  vi.spyOn(proto, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return isChat(this) && !hidden ? SEEN : 0;
  });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const box = this.closest("[aria-label='Mensajes de la conversación']");
    if (!box || hidden) return new DOMRect();
    if (this === box) return new DOMRect(0, boxTop, 300, SEEN);
    const rows = rowsOf(box);
    const above = rows.slice(0, rows.indexOf(this as HTMLLIElement));
    const y = boxTop + above.reduce((sum, li) => sum + tall(li), 0) - box.scrollTop;
    return new DOMRect(0, y, 300, tall(this));
  });
  Object.defineProperty(proto, "scrollTop", {
    configurable: true,
    get(this: HTMLElement) {
      return tops.get(this) ?? 0;
    },
    set(this: HTMLElement, value: number) {
      const next = Math.max(0, Math.min(value, full(this) - SEEN));
      if (hidden || next === (tops.get(this) ?? 0)) return;
      tops.set(this, next);
      queueMicrotask(() => this.dispatchEvent(new Event("scroll")));
    },
  });
  proto.scrollTo = scrollTo as unknown as typeof proto.scrollTo;
  scrollTo.mockClear();
  boxTop = 40; // la región empieza más abajo en la página
  hidden = false;
  grown.clear();
  observers.length = 0;
  vi.stubGlobal("ResizeObserver", Observer);
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// Páginas de `size` como la API (del más nuevo al más antiguo; el cursor, el más antiguo de la
// página): al releer, TanStack pide la siguiente desde la primera nueva, como en la pila real.
function server(texts: Message[], size = 50) {
  const state = { texts, sending: null as null | (() => void), hold: null as null | Promise<void> };
  const api = mockApi({
    [`GET ${BASE}/`]: { status: 200, body: { results: [rosa], next: null } },
    [`GET ${BASE}/1/`]: { status: 200, body: rosa },
  });
  const inner = api.getMockImplementation()!;
  api.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://x");
    if (url.pathname !== `${BASE}/1/messages/`) return inner(input, init);
    if (init?.method === "POST") {
      const { client_message_id, body } = JSON.parse(String(init.body));
      const sent = { ...message("s1", body, true), client_message_id } as Message;
      await new Promise<void>((resolve) => (state.sending = resolve));
      state.texts = [...state.texts, sent];
      return new Response(JSON.stringify(sent), { status: 202 });
    }
    const cursor = url.searchParams.get("cursor");
    if (cursor && state.hold) await state.hold;
    const end = cursor ? state.texts.findIndex(({ id }) => id === cursor) : state.texts.length;
    const results = state.texts.slice(Math.max(0, end - size), end);
    const next = end > size ? results[0]!.id : null;
    return new Response(JSON.stringify({ results: results.toReversed(), next }));
  });
  return Object.assign(state, { api });
}
const history = (n: number) =>
  Array.from({ length: n }, (_, i) => message(`h${i + 1}`, `Historial ${i + 1}`));
const chat = () => screen.getByRole("region", { name: "Mensajes de la conversación" });
const top = () => chat().scrollTop;
const bottom = () => full(chat()) - SEEN;
const live = () => chat().previousElementSibling!.querySelector("[role=status]")!;
const unseen = () => screen.queryByRole("button", { name: /^\d+\+? mensajes? nuevos?$/ });
const at = (text: string) => screen.getByText(text).closest("li")!.getBoundingClientRect().top;
const tick = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)));
async function open(last = "Historial 5") {
  const view = renderApp(
    <TenantProvider value={tenant}>
      <Inbox />
    </TenantProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  await screen.findByText(last);
  await waitFor(() => expect(top()).toBe(bottom())); // abre al final
  return view;
}
async function arrive(state: { texts: Message[] }, ...more: Message[]) {
  state.texts = [...state.texts, ...more];
  fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
  await screen.findByText(more.at(-1)!.body!);
  await tick();
}
// Un cambio de ancho sin cambio de alto, antes del `scroll` de quien acaba de desplazarse: no se toca.
const widen = async (value: number) => {
  act(() => void ((chat().scrollTop = value), resized()));
  await tick();
  expect(top()).toBe(value);
};
const scroll = async (value: number) => {
  act(() => void (chat().scrollTop = value));
  await tick();
};

describe("seguir lo nuevo y «N mensajes nuevos» (UX-INBOX-06b)", () => {
  it("al final, lo nuevo se ve aunque salga lo antiguo, y el botón nunca aparece", async () => {
    const state = server(history(5), 5); // una página: entra uno, sale el más antiguo
    await open();
    const shown: boolean[] = [];
    const watcher = new MutationObserver(() => shown.push(!!unseen()));
    watcher.observe(document.body, { subtree: true, childList: true, characterData: true });
    await arrive(state, message("n1", "Nuevo largo"));
    watcher.disconnect();
    expect(top()).toBe(bottom());
    expect(shown).not.toContain(true);
    // Entra uno y sale otro del mismo alto: la vista no cambia ni hay `scroll`, y aun así se vio.
    await arrive(state, message("n2", "Nuevo 2"));
    expect(top()).toBe(bottom());
    await scroll(0);
    await arrive(state, message("n3", "Nuevo 3"));
    expect(unseen()).toHaveTextContent("1 mensaje nuevo"); // «Nuevo 2» ya se vio
  });

  it("leyendo el historial la vista no se mueve; el botón cuenta lo ajeno y lleva al final", async () => {
    const state = server(history(9));
    await open("Historial 9");
    await scroll(0);
    await arrive(state, message("n1", "Nuevo 1"), message("o1", "Mío", true));
    await arrive(state, message("n2", "Nuevo 2"));
    expect(top()).toBe(0);
    const button = unseen()!;
    expect(button).toHaveTextContent("2 mensajes nuevos"); // lo propio no cuenta
    await waitFor(() => expect(live()).toHaveTextContent("2 mensajes nuevos")); // se anuncia, cortés
    expect(button.querySelector("svg")).not.toBeNull(); // icono y texto, sin texto del cliente
    await arrive(state, message("n3", "Nuevo 3"));
    expect(unseen()).toHaveTextContent("3 mensajes nuevos");
    fireEvent.click(unseen()!);
    expect(scrollTo).toHaveBeenCalledWith({ top: full(chat()), behavior: "smooth" });
    expect(from.at(-1)).toBe(full(chat()) - 2 * SEEN); // de lejos: salto a dos pantallas, luego suave
    expect(chat()).toHaveFocus(); // el botón se va; el foco no se pierde
    act(() => glide());
    await waitFor(() => expect(unseen()).toBeNull());
    expect(top()).toBe(bottom());
    await arrive(state, message("n4", "Nuevo 4")); // otra vez al final: se sigue
    expect(top()).toBe(bottom());
    expect(unseen()).toBeNull();
  });

  it("con dos páginas, lo que sale arriba al releer no mueve lo que se lee ni infla la cuenta", async () => {
    const state = server(history(10), 5);
    await open("Historial 10");
    fireEvent.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
    await screen.findByText("Historial 1");
    await scroll(250); // «Historial 5» es la última fila a la vista
    const before = at("Historial 5");
    await arrive(state, message("n1", "Nuevo 1"), message("n2", "Nuevo 2"));
    expect(at("Historial 5")).toBe(before);
    expect(unseen()).toHaveTextContent("2 mensajes nuevos");
  });

  it("un envío propio no es «lo último visto»: lo que llega después sí se cuenta", async () => {
    const state = server(history(5));
    await open();
    const box = screen.getByRole("textbox", { name: "Responder al cliente" });
    fireEvent.change(box, { target: { value: "Hola Rosa" } });
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    await screen.findByText("Hola Rosa");
    await tick();
    expect(top()).toBe(bottom()); // el envío propio se ve
    await scroll(0);
    await act(async () => state.sending?.());
    await waitFor(() => expect(state.texts.at(-1)!.id).toBe("s1"));
    await arrive(state, message("n1", "Nuevo 1"));
    expect(unseen()).toHaveTextContent("1 mensaje nuevo");
  });

  it("si la región se mueve en la página sin desplazarse, lo que llega no la mueve", async () => {
    const state = server(history(9));
    await open("Historial 9");
    await scroll(100);
    boxTop = -170; // cabecera compacta, teclado o un aviso que crece: sin `scroll`
    await arrive(state, message("n1", "Nuevo 1"));
    expect(top()).toBe(100);
  });

  it("lo que llega mientras el botón baja también se alcanza; si quien lee sube, no", async () => {
    const state = server(history(9));
    await open("Historial 9");
    await scroll(0);
    await arrive(state, message("n1", "Nuevo 1"));
    fireEvent.click(unseen()!); // a medio camino (el navegador cancela el suave al asignar)
    await arrive(state, message("n2", "Nuevo 2"));
    expect(top()).toBe(bottom());
    expect(unseen()).toBeNull();
    await scroll(0);
    await arrive(state, message("n3", "Nuevo 3"));
    fireEvent.click(unseen()!);
    await tick();
    await scroll(100); // frena el suave subiendo
    await arrive(state, message("n4", "Nuevo 4"));
    expect(top()).toBe(100);
    for (const stop of ["wheel", "touchStart", "pointerDown"] as const) {
      fireEvent.click(unseen()!);
      fireEvent[stop](chat()); // la rueda, un toque o la barra frenan el suave
      const stay = top() + 10; // y baja a mano sin llegar al final
      await scroll(stay);
      await arrive(state, message(stop, `Parado ${stop}`));
      expect(top()).toBe(stay); // sin tirón hacia el final
    }
  });

  it("si la fila leída sale al releer, se ancla en la siguiente que sigue", async () => {
    const state = server(history(10), 5);
    await open("Historial 10");
    fireEvent.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
    await screen.findByText("Historial 1");
    await scroll(100); // a la vista, de «Historial 2» a «Historial 4»
    await arrive(state, ...[1, 2, 3, 4].map((n) => message(`n${n}`, `Nuevo ${n}`)));
    expect(top()).toBe(0); // «Historial 5», que seguía, queda arriba
  });

  it("una relectura que cancela «Cargar mensajes anteriores» no mueve la vista", async () => {
    const state = server(history(10), 5);
    const { client } = await open("Historial 10");
    await scroll(150);
    const before = at("Historial 10");
    state.hold = new Promise(() => {}); // la página anterior no llega
    fireEvent.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
    state.texts = [...state.texts, message("n1", "Nuevo 1")];
    await act(() => client.invalidateQueries());
    await screen.findByText("Nuevo 1");
    expect(at("Historial 10")).toBe(before); // sale «Historial 6» de arriba; lo que se lee no se mueve
  });

  it("si lo último visto ya no está (llegó más de una página), «50+»", async () => {
    const state = server(history(5), 5);
    await open();
    await scroll(0);
    await arrive(state, ...history(6).map(({ id }, i) => message(`m${id}`, `Otro ${i}`)));
    expect(unseen()).toHaveTextContent("50+ mensajes nuevos");
    await waitFor(() => expect(live()).toHaveTextContent("50+ mensajes nuevos"));
  });

  it("con movimiento reducido, sin desplazamiento suave", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduced") }));
    const state = server(history(9));
    await open("Historial 9");
    await scroll(0);
    await arrive(state, message("n1", "Nuevo 1"));
    fireEvent.click(unseen()!);
    expect(scrollTo).toHaveBeenCalledWith({ top: full(chat()), behavior: "auto" });
    expect(from.at(-1)).toBe(0); // un solo salto, sin el previo
  });

  it("cargar los anteriores conserva la posición y no muestra el botón", async () => {
    server(history(10), 5);
    await open("Historial 10");
    await scroll(50);
    fireEvent.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
    await screen.findByText("Historial 1");
    await waitFor(() => expect(top()).toBe(550)); // arriba crecieron 500 px: lo leído no se mueve
    expect(unseen()).toBeNull();
  });

  it("un alto que cambia sin filas nuevas no mueve la fila leída; al final, se sigue", async () => {
    const state = server(history(9));
    const { unmount } = await open("Historial 9");
    grown.set("Historial 9", 40); // la última crece (un estado de entrega): se sigue el final
    act(resized);
    await tick();
    expect(top()).toBe(bottom());
    await scroll(100);
    const before = at("Historial 4");
    grown.set("Historial 1", 60); // arriba crece algo (un aviso, un adjunto): sin filas nuevas
    act(resized);
    await tick();
    expect(at("Historial 4")).toBe(before);
    expect(top()).toBe(160);
    await widen(100); // tras colocar por el observador
    await arrive(state, message("n1", "Nuevo 1"));
    expect(top()).toBe(100); // lo de siempre: lo que llega no mueve la vista
    await widen(0); // tras colocar por la llegada
    fireEvent.click(unseen()!); // el botón baja y, a medio camino, algo crece: se sigue igual
    grown.set("Nuevo 1", 50);
    act(resized);
    await tick();
    expect(top()).toBe(bottom());
    expect(watching().length).toBeGreaterThan(0);
    unmount(); // al cerrar el chat (o cambiar de conversación, que lo vuelve a montar), sin fugas
    expect(observers.filter(({ off }) => !off)).toEqual([]);
  });

  it("el contador cuenta solo lo que queda bajo el borde visible, sin pedir nada", async () => {
    const state = server(history(9));
    await open("Historial 9");
    await scroll(0);
    await arrive(state, message("n1", "Nuevo 1"), message("n2", "Nuevo 2"));
    expect(unseen()).toHaveTextContent("2 mensajes nuevos");
    const calls = state.api.mock.calls.length;
    await scroll(700); // «Nuevo 1» a la vista; «Nuevo 2» empieza justo en el borde: debajo
    expect(unseen()).toHaveTextContent("1 mensaje nuevo");
    await scroll(752); // «Nuevo 2» asoma 52 px: bajo el botón (44 + 8), aún sin leer
    expect(unseen()).toHaveTextContent("1 mensaje nuevo");
    await scroll(bottom() - 30); // a 30 px del final (no es «el final»), los dos a la vista
    expect(unseen()).toBeNull();
    await scroll(0); // volver a subir no los vuelve a contar
    expect(unseen()).toBeNull();
    expect(state.api.mock.calls.length).toBe(calls); // ver no marca nada en el servidor
    await arrive(state, message("n3", "Nuevo 3"));
    expect(unseen()).toHaveTextContent("1 mensaje nuevo");
  });

  it("la burbuja pendiente a la vista no pasa a ser lo último visto", async () => {
    const state = server(history(5));
    await open();
    const box = screen.getByRole("textbox", { name: "Responder al cliente" });
    fireEvent.change(box, { target: { value: "Hola Rosa" } });
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    await screen.findByText("Hola Rosa");
    await tick();
    await scroll(bottom() - 30); // deja el final
    await scroll(bottom() - 20); // la burbuja pendiente, a la vista
    await act(async () => state.sending?.());
    await waitFor(() => expect(state.texts.at(-1)!.id).toBe("s1"));
    await arrive(state, message("n1", "Nuevo 1"));
    expect(unseen()).toHaveTextContent("1 mensaje nuevo"); // no «50+»
  });

  it("el botón va antes que la región en el orden de foco y se anuncia con su número", async () => {
    const state = server(history(9));
    await open("Historial 9");
    await scroll(0);
    const overlay = chat().previousElementSibling!;
    const status = live(); // existe y está vacía antes: se anuncia el cambio
    expect(status).toBeEmptyDOMElement();
    expect(status).toHaveClass("sr-only");
    expect(overlay).toHaveClass("pointer-events-none", "absolute"); // superpuesta: deja pasar la rueda
    expect(chat().parentElement).toHaveClass("relative");
    expect(chat()).not.toHaveClass("scroll-pb-16");
    await arrive(state, message("n1", "Nuevo 1"));
    const button = unseen()!;
    expect(live()).toBe(status); // la misma región, no una nueva
    expect(button.closest("[role=status], [aria-live]")).toBeNull(); // su cuenta no se anuncia sola
    expect(chat()).toHaveClass("scroll-pb-16"); // un control enfocado no queda bajo el botón
    expect(button.compareDocumentPosition(chat()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(chat()).not.toContainElement(button);
    expect(button.closest("[aria-hidden=true]")).toBeNull();
    expect(button).not.toHaveAttribute("tabindex", "-1");
    await waitFor(() => expect(status).toHaveTextContent(/^1 mensaje nuevo$/));
  });
});

// UX-INBOX-06e. El anuncio espera `SAY` (500 ms): ese plazo corre en un reloj a mano (`wait`, o
// `say()` para soltarlo ya); los demás temporizadores (TanStack, `waitFor`) siguen siendo reales.
describe("anuncios del contador y ancla ante crecimiento dentro de la vista (UX-INBOX-06e)", () => {
  const held = new Map<number, { at: number; fn: () => void }>();
  let clock = 0;
  const wait = (ms: number) =>
    act(() => {
      clock += ms;
      for (const [id, { at, fn }] of held) if (at <= clock) void (held.delete(id), fn());
    });
  const say = () => wait(1e9);
  const hold = () => {
    const [set, clear] = [globalThis.setTimeout, globalThis.clearTimeout];
    let next = 1e6;
    vi.spyOn(globalThis, "setTimeout").mockImplementation(((fn: () => void, ms?: number) => {
      if (ms !== 500) return set(fn, ms);
      held.set(++next, { at: clock + ms, fn });
      return next;
    }) as typeof setTimeout);
    vi.spyOn(globalThis, "clearTimeout").mockImplementation((id) =>
      held.has(id as number) ? void held.delete(id as number) : clear(id),
    );
  };
  beforeEach(() => void (held.clear(), (clock = 0)));

  it("cada cambio reinicia el plazo: a 499 ms no se dice nada; al cerrar, nada queda pendiente", async () => {
    const state = server(history(9));
    const { unmount } = await open("Historial 9");
    hold();
    await scroll(0);
    await arrive(state, message("n1", "Nuevo 1"));
    await wait(499);
    await arrive(state, message("n2", "Nuevo 2")); // a 499 ms de la primera: el plazo vuelve a empezar
    await wait(499);
    expect(live()).toBeEmptyDOMElement();
    await wait(1);
    expect(live()).toHaveTextContent(/^2 mensajes nuevos$/);
    const said: string[] = [];
    const watcher = new MutationObserver(() => said.push(live().textContent!));
    watcher.observe(live(), { subtree: true, childList: true, characterData: true });
    for (const n of [3, 4, 5]) {
      await arrive(state, message(`n${n}`, `Nuevo ${n}`));
      await wait(400);
    }
    await wait(99); // una ráfaga de 800 ms: nada hasta 500 ms tras la última llegada
    expect(live()).toBeEmptyDOMElement();
    await wait(1);
    watcher.disconnect();
    expect(said.filter((text, i) => text !== said[i - 1])).toEqual(["", "5 mensajes nuevos"]);
    await arrive(state, message("n6", "Nuevo 6"));
    expect(held.size).toBe(1);
    unmount();
    expect(held.size).toBe(0); // nada se dispara después de cerrar el chat
  });

  it("se oye la llegada, no cada paso al bajar leyendo; una ráfaga, una vez", async () => {
    const state = server(history(9));
    await open("Historial 9");
    hold();
    await scroll(0);
    const said: string[] = []; // lo que dice la región, cada vez que cambia
    const watcher = new MutationObserver(() => said.push(live().textContent!));
    watcher.observe(live(), { subtree: true, childList: true, characterData: true });
    await arrive(state, ...[1, 2, 3].map((n) => message(`n${n}`, `Nuevo ${n}`)));
    expect(live()).toBeEmptyDOMElement(); // aún no: espera por si llega más
    await say();
    expect(live()).toHaveTextContent(/^3 mensajes nuevos$/);
    expect(unseen()).toHaveAccessibleName("3 mensajes nuevos"); // el nombre es lo que se ve
    expect(unseen()).not.toHaveAttribute("aria-describedby");
    await scroll(700); // «Nuevo 1», leído
    expect(unseen()).toHaveTextContent("2 mensajes nuevos"); // lo visible sigue cada paso
    await scroll(800);
    expect(unseen()).toHaveTextContent("1 mensaje nuevo");
    await say();
    expect(live()).toBeEmptyDOMElement();
    await arrive(state, message("n4", "Nuevo 4"), message("n5", "Nuevo 5")); // otra vez 3
    await say();
    expect(live()).toHaveTextContent(/^3 mensajes nuevos$/);
    await arrive(state, message("n6", "Nuevo 6")); // 4 y enseguida 5: se oye solo el 5
    await arrive(state, message("n7", "Nuevo 7"));
    await say();
    await arrive(state, message("n8", "Nuevo 8")); // llega y se lee antes del plazo: la llegada
    await scroll(900); // se oye igual, con la cuenta de entonces
    await say();
    expect(unseen()).toHaveTextContent("5 mensajes nuevos");
    await scroll(bottom());
    await say();
    watcher.disconnect();
    expect(unseen()).toBeNull();
    expect(said.filter((text, i) => text !== said[i - 1])).toEqual([
      ...["3 mensajes nuevos", "", "3 mensajes nuevos", "", "5 mensajes nuevos"],
      ...["", "5 mensajes nuevos", ""], // en 0, vacía y callada
    ]);
  });

  it("lo que crece dentro de la vista empuja hacia abajo; encima, se compensa", async () => {
    const state = server(history(9));
    await open("Historial 9");
    await scroll(150); // arriba asoma la mitad de «Historial 2»
    const [two, three, four] = ["Historial 2", "Historial 3", "Historial 4"].map(at);
    grown.set("Historial 3", 40); // dentro de la vista
    act(resized);
    await tick();
    expect([at("Historial 2"), at("Historial 3"), at("Historial 4")]).toEqual([
      two,
      three,
      four! + 40,
    ]);
    expect(top()).toBe(150);
    grown.set("Historial 2", 30); // la de arriba, la que asoma, crece por abajo: tampoco sube nada
    act(resized);
    await tick();
    expect(top()).toBe(150);
    await scroll(230); // «Historial 2» (130 px) termina justo en el borde: ya no se ve
    const five = at("Historial 5");
    grown.set("Historial 2", 60);
    act(resized);
    await tick();
    expect(at("Historial 5")).toBe(five);
    expect(top()).toBe(260);
    await arrive(state, message("n1", "Nuevo 1")); // lo de siempre: lo que llega no mueve la vista
    expect(at("Historial 5")).toBe(five);
  });

  it("tras crecer dentro de la vista, si la de arriba sale al releer, no salta", async () => {
    const state = server(history(15), 5);
    await open("Historial 15");
    for (const n of [6, 1]) {
      fireEvent.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
      await screen.findByText(`Historial ${n}`);
    }
    await scroll(150); // de «Historial 2» (asoma) a «Historial 5»
    grown.set("Historial 3", 120); // dentro de la vista, sin `scroll`
    act(resized);
    await tick();
    await arrive(state, message("n1", "Nuevo 1"), message("n2", "Nuevo 2")); // salen 1 y 2
    expect(top()).toBe(0); // «Historial 4», donde estaba (hasta donde se puede); no 70 px más abajo
  });

  it("oculto, el ancla no se mide; al volver a verse, la fila leída sigue donde estaba", async () => {
    server(history(9));
    await open("Historial 9");
    await scroll(100);
    hidden = true; // `display: none` mientras se ve al cliente
    act(resized);
    await tick();
    hidden = false;
    act(resized);
    await tick();
    expect(top()).toBe(100);
  });
});
