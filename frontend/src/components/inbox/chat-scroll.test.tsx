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
const scrollTo = vi.fn(function (this: HTMLElement, { top, behavior }: ScrollToOptions) {
  from.push(this.scrollTop);
  glide = () => void (this.scrollTop = top!);
  if (behavior === "smooth") this.scrollTop += (top! - SEEN - this.scrollTop) / 2;
  else glide();
});
beforeEach(() => {
  const proto = HTMLElement.prototype;
  vi.spyOn(proto, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return isChat(this) ? full(this) : 0;
  });
  vi.spyOn(proto, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return isChat(this) ? SEEN : 0;
  });
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const box = this.closest("[aria-label='Mensajes de la conversación']");
    if (!box) return new DOMRect();
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
      if (next === (tops.get(this) ?? 0)) return;
      tops.set(this, next);
      queueMicrotask(() => this.dispatchEvent(new Event("scroll")));
    },
  });
  proto.scrollTo = scrollTo as unknown as typeof proto.scrollTo;
  scrollTo.mockClear();
  boxTop = 40; // la región empieza más abajo en la página
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
    expect(button.closest("[role=status]")).not.toBeNull(); // se anuncia, cortés
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
    await arrive(state, message("n1", "Nuevo 1"));
    const button = unseen()!;
    expect(button.compareDocumentPosition(chat()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(chat()).not.toContainElement(button);
    expect(button.closest("[aria-hidden=true]")).toBeNull();
    expect(button).not.toHaveAttribute("tabindex", "-1");
    expect(button.closest("[role=status]")).toHaveTextContent(/^1 mensaje nuevo$/);
  });
});
