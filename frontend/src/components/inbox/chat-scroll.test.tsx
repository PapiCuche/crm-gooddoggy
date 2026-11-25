import { act, configure, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Message, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

// UX-INBOX-06: seguir lo nuevo al final y el botón «N mensajes nuevos». jsdom no maqueta: el alto de
// la región de mensajes se simula (100 px por mensaje, 300 px visibles); lo real se midió en Chrome.
configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const BASE = "/api/v1/o/acme/conversations";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  ...{ membership_id: "m1", mfa: { required: false, missing: false }, roles: [] },
  permissions: [{ code: "conversations.view", scopes: [] }],
};
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, version: 1 },
  channel: {
    ...{ id: "ch1", platform: "SANDBOX", name: "Sandbox", text_max: 4096 },
    ...{ text_unit: "CHARACTERS", accepts_media: true },
  },
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

// La región de mensajes con alto simulado; asignar `scrollTop` la desplaza (con tope) y avisa con
// `scroll`, como un navegador.
const SEEN = 300;
const tops = new WeakMap<Element, number>();
const isChat = (el: Element) => el.getAttribute("aria-label") === "Mensajes de la conversación";
const full = (el: Element) => el.querySelectorAll("li").length * 100;
const scrollTo = vi.fn(function (this: HTMLElement, { top }: ScrollToOptions) {
  this.scrollTop = top!;
});
beforeEach(() => {
  const proto = HTMLElement.prototype;
  vi.spyOn(proto, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    return isChat(this) ? full(this) : 0;
  });
  vi.spyOn(proto, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return isChat(this) ? SEEN : 0;
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
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function server(texts: Message[], older?: Message[]) {
  const state = { texts };
  mockApi({
    [`GET ${BASE}/`]: { status: 200, body: { results: [rosa], next: null } },
    [`GET ${BASE}/1/`]: { status: 200, body: rosa },
    [`GET ${BASE}/1/messages/`]: () => ({
      status: 200,
      body: { results: [...state.texts].reverse(), next: older ? "p2" : null },
    }),
    [`GET ${BASE}/1/messages/?cursor=p2`]: () => ({
      status: 200,
      body: { results: [...(older ?? [])].reverse(), next: null },
    }),
  });
  return state;
}
const history = (n: number) =>
  Array.from({ length: n }, (_, i) => message(`h${i}`, `Historial ${i + 1}`));
const chat = () => screen.getByRole("region", { name: "Mensajes de la conversación" });
const top = () => chat().scrollTop;
const bottom = () => full(chat()) - SEEN;
const unseen = () => screen.queryByRole("button", { name: /mensajes? nuevos?/ });
async function open() {
  renderApp(
    <TenantProvider value={tenant}>
      <Inbox />
    </TenantProvider>,
  );
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  await screen.findByText("Historial 5");
  await waitFor(() => expect(top()).toBe(bottom())); // abre al final
}
async function arrive(state: { texts: Message[] }, ...more: Message[]) {
  state.texts = [...state.texts, ...more];
  fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
  await screen.findByText(more.at(-1)!.body!);
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
}
const readHistory = async () => {
  act(() => void (chat().scrollTop = 0));
  await waitFor(() => expect(top()).toBe(0));
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
};

describe("seguir lo nuevo y «N mensajes nuevos» (UX-INBOX-06)", () => {
  it("al final, un mensaje nuevo se ve sin tocar nada y no hay botón", async () => {
    const state = server(history(5));
    await open();
    await arrive(state, message("n1", "Nuevo 1"));
    expect(top()).toBe(bottom());
    expect(top()).toBe(300); // seis mensajes: 600 px, se ven los últimos 300
    expect(unseen()).toBeNull();
  });

  it("leyendo el historial la vista no se mueve; el botón cuenta lo ajeno y lleva al final", async () => {
    const state = server(history(5));
    await open();
    await readHistory();
    await arrive(state, message("n1", "Nuevo 1"), message("o1", "Mío", true));
    await arrive(state, message("n2", "Nuevo 2"));
    expect(top()).toBe(0);
    const button = unseen()!;
    expect(button).toHaveTextContent("2 mensajes nuevos"); // lo propio no cuenta
    expect(button.closest("[role=status]")).not.toBeNull(); // se anuncia, cortés
    expect(button.querySelector("svg")).not.toBeNull(); // icono y texto
    await arrive(state, message("n3", "Nuevo 3"));
    expect(unseen()).toHaveTextContent("3 mensajes nuevos");
    fireEvent.click(unseen()!);
    expect(scrollTo).toHaveBeenCalledWith({ top: full(chat()), behavior: "smooth" });
    expect(chat()).toHaveFocus(); // el botón se va; el foco no se pierde
    await waitFor(() => expect(unseen()).toBeNull());
    expect(top()).toBe(bottom());
    await arrive(state, message("n4", "Nuevo 4")); // otra vez al final: se sigue
    expect(top()).toBe(bottom());
    expect(unseen()).toBeNull();
  });

  it("uno solo dice «1 mensaje nuevo»; con movimiento reducido, sin desplazamiento suave", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("reduced") }));
    const state = server(history(5));
    await open();
    await readHistory();
    await arrive(state, message("n1", "Nuevo 1"));
    expect(unseen()).toHaveTextContent("1 mensaje nuevo");
    fireEvent.click(unseen()!);
    expect(scrollTo).toHaveBeenCalledWith({ top: full(chat()), behavior: "auto" });
  });

  it("cargar los anteriores conserva la posición y no muestra el botón", async () => {
    server(history(5), [message("o1", "Antiguo 1"), message("o2", "Antiguo 2")]);
    await open();
    await readHistory();
    act(() => void (chat().scrollTop = 50));
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    fireEvent.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
    await screen.findByText("Antiguo 1");
    await waitFor(() => expect(top()).toBe(250)); // lo de arriba creció 200 px: lo leído no se mueve
    expect(unseen()).toBeNull();
  });
});
