import {
  act,
  cleanup,
  configure,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Grant, Message, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

const BASE = "/api/v1/o/acme/conversations";
const LIST = `GET ${BASE}/`;
const DETAIL = `GET ${BASE}/1/`;
const MESSAGES = `GET ${BASE}/1/messages/`;
const SEND = `POST ${BASE}/1/messages/`;
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [],
};
const grants = (...codes: string[]): Grant[] =>
  codes.map((code) => ({ code, scopes: ["ORGANIZATION" as const] }));
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" } },
  ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" } },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null, version: 1 },
  ...{ last_message_preview: null, last_message_direction: null },
} as unknown as Conversation;
const saved = (id: string, body: string, changes: Partial<Message> = {}) =>
  ({
    ...{ id, direction: "OUTBOUND", sender_type: "USER", sender_user_id: "u1" },
    ...{ message_type: "TEXT", body, client_message_id: `cm-${id}`, delivery_status: "QUEUED" },
    ...{ created_at: "2026-11-04T16:00:00Z", sent_at: null, delivered_at: null },
    ...{ read_at: null, failed_at: null, ...changes },
  }) as Message;

type Reply = { status: number; body?: unknown };
// El servidor: lo que responde al envío y lo que lista; el envío puede quedar en espera.
function server(options: { conversation?: Partial<Conversation>; send?: () => Reply } = {}) {
  const conversation = { ...rosa, ...options.conversation };
  const stored: Message[] = [];
  let release = () => {};
  let hold: Promise<void> | null = null;
  const api = mockApi({
    [LIST]: page([conversation]),
    [DETAIL]: { status: 200, body: conversation },
    [MESSAGES]: () => page([...stored].reverse()),
    [SEND]: () => {
      const reply = options.send?.() ?? { status: 202, body: saved("s1", "ok") };
      if (reply.status < 300) stored.push(reply.body as Message);
      return reply;
    },
  });
  const inner = api.getMockImplementation()!;
  api.mockImplementation(async (input, init) => {
    if (init?.method === "POST") await hold;
    return inner(input, init);
  });
  const posts = () =>
    api.mock.calls
      .filter(([url, init]) => String(url).endsWith("/1/messages/") && init?.method === "POST")
      .map(
        ([, init]) => JSON.parse(String(init!.body)) as { client_message_id: string; body: string },
      );
  return {
    api,
    stored,
    wait: () => void (hold = new Promise<void>((resolve) => (release = resolve))),
    go: () => release(),
    ids: () => posts().map((post) => post.client_message_id) as [string, string, string],
    posts,
  };
}
const show = (permissions: Grant[] = grants("conversations.view", "messages.send")) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  await screen.findByRole("heading", { level: 2 }); // el chat se abrió (su lista puede estar vacía)
};
const box = () => screen.findByRole("textbox", { name: "Responder al cliente" });
const type = async (text: string) => fireEvent.change(await box(), { target: { value: text } });
// Lo que un envío indebido haría llega tras unas esperas asíncronas: se deja pasar antes de afirmar.
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 30)));
const sendButton = (name = "Enviar") => screen.getByRole("button", { name });

// Estos tests esperan a la API simulada (la lista, el detalle y los mensajes) y al montaje del chat:
// en una máquina cargada (CI, el gate) el segundo por omisión no alcanza. Se sube lo que esperan los
// `findBy…` y `waitFor` de este archivo y el tiempo de cada test, sin reintentar nada: la espera
// termina en cuanto aparece lo esperado.
configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("cuadro de redacción (F6-04c)", () => {
  const OWN = [
    { code: "messages.send", scopes: ["OWN" as const] },
    ...grants("conversations.view"),
  ];
  const TEAM = [
    { code: "messages.send", scopes: ["TEAM" as const] },
    ...grants("conversations.view"),
  ];
  it.each([
    ["sin `messages.send` no hay cuadro", {}, grants("conversations.view"), false],
    // Sin alcance concedido (lista vacía) falla cerrado, como la API, aunque la conversación sea mía.
    [
      "con una lista de alcances vacía falla cerrado",
      { assigned_user_id: "u1" },
      [{ code: "messages.send", scopes: [] }, ...grants("conversations.view")],
      false,
    ],
    [
      "con alcance OWN no se ofrece en una conversación ajena",
      { assigned_user_id: "u2" },
      OWN,
      false,
    ],
    ["con alcance OWN se ofrece en la propia", { assigned_user_id: "u1" }, OWN, true],
    // Con equipo o sucursal no se sabe desde aquí: se ofrece y la API decide.
    ["con alcance de equipo se ofrece", { assigned_user_id: "u2" }, TEAM, true],
  ])("%s", async (_name, conversation, permissions, offered) => {
    server({ conversation });
    show(permissions);
    await openChat();
    if (offered) {
      expect(await box()).toBeVisible();
    } else {
      expect(await screen.findByText(/No tienes permiso para responder/)).toBeVisible();
      expect(screen.queryByRole("textbox")).toBeNull();
    }
  });

  it("envía con un id nuevo, muestra «En cola» al instante, limpia, enfoca y refresca", async () => {
    const hostile = "<b>hola</b> ‮abc";
    const s = server({ send: () => ({ status: 202, body: saved("s1", hostile) }) });
    show();
    await openChat();
    s.wait();
    await type(hostile);
    fireEvent.click(sendButton());
    // Optimista: ya está en la lista, en cola, y el cuadro no se pierde mientras tanto.
    const log = await screen.findByRole("list", { name: "Mensajes" });
    expect(await within(log).findByText("En cola")).toBeVisible();
    expect(log.querySelector("bdi")?.textContent).toBe(hostile);
    expect(log.querySelector("b")).toBeNull(); // texto, nunca HTML
    expect(sendButton("Enviando…")).toHaveAttribute("aria-disabled", "true");
    expect(await box()).toHaveAttribute("readonly");
    s.go();
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    expect(await box()).toHaveFocus();
    expect(s.posts()).toEqual([
      { client_message_id: expect.stringMatching(/^[0-9a-f-]{36}$/), body: hostile },
    ]);
    expect(within(log).getAllByRole("listitem")).toHaveLength(1); // sin duplicar tras refrescar
    // El siguiente mensaje lleva otro id.
    await type(hostile); // el mismo texto otra vez es otro mensaje: otro id
    fireEvent.click(sendButton());
    await waitFor(() => expect(s.posts()).toHaveLength(2));
    const [first, second] = s.ids();
    expect(second).not.toBe(first);
  });

  it("si falla conserva el texto, quita el mensaje en cola y «Reintentar» reutiliza el mismo id", async () => {
    let reply: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    const s = server({ send: () => reply });
    show();
    await openChat();
    await type("Hola Rosa");
    fireEvent.click(sendButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(await box()).toHaveValue("Hola Rosa");
    expect(screen.queryByText("En cola")).toBeNull();
    reply = { status: 202, body: saved("s1", "Hola Rosa") };
    fireEvent.click(sendButton("Reintentar"));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    await waitFor(() => expect(box()).resolves.toHaveValue(""));
    const [a, b] = s.ids();
    expect(b).toBe(a); // el servidor no lo duplica
    expect(screen.getAllByText("Hola Rosa")).toHaveLength(1);
  });

  it("editar el texto tras un fallo empieza un intento con otro id", async () => {
    const s = server({ send: () => ({ status: 500, body: { code: "INTERNAL_ERROR" } }) });
    show();
    await openChat();
    await type("uno");
    fireEvent.click(sendButton());
    await screen.findByRole("alert");
    await type("uno y dos");
    fireEvent.click(sendButton());
    await waitFor(() => expect(s.posts()).toHaveLength(2));
    const [one, two] = s.ids();
    expect(two).not.toBe(one);
  });

  it("explica 409 y 404 sin perder el texto; un id ocupado se renueva", async () => {
    let reply: Reply = { status: 409, body: { code: "CHANNEL_UNAVAILABLE" } };
    const s = server({ send: () => reply });
    show();
    await openChat();
    await type("Hola");
    fireEvent.click(sendButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El canal de esta conversación no está conectado",
    );
    expect(await box()).toHaveValue("Hola");
    reply = { status: 409, body: { code: "CLIENT_MESSAGE_ID_TAKEN" } };
    fireEvent.click(sendButton("Reintentar"));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Ese id de mensaje ya se usó"),
    );
    reply = { status: 404, body: { code: "NOT_FOUND" } };
    fireEvent.click(sendButton("Reintentar"));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "No puedes responder en esta conversación",
      ),
    );
    expect(await box()).toHaveValue("Hola");
    const [first, again, renewed] = s.ids();
    expect(again).toBe(first);
    expect(renewed).not.toBe(again); // tras CLIENT_MESSAGE_ID_TAKEN
  });

  it("un 403 se explica como el 404 y un segundo envío mientras el primero espera no sale", async () => {
    const s = server({ send: () => ({ status: 403, body: { code: "PERMISSION_DENIED" } }) });
    show();
    await openChat();
    s.wait();
    await type("Hola");
    fireEvent.click(sendButton());
    fireEvent.click(await screen.findByRole("button", { name: "Enviando…" })); // doble clic
    const said = screen.getAllByRole("status").map((one) => one.textContent);
    expect(said).toContain("Enviando…"); // se anuncia, no solo cambia el botón
    fireEvent.keyDown(await box(), { key: "Enter" });
    s.go();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No puedes responder en esta conversación",
    );
    expect(await box()).toHaveValue("Hola");
    expect(s.posts()).toHaveLength(1);
  });

  it.each(["CONVERSATION_CLOSED", "CHANNEL_UNAVAILABLE", "CONTACT_BLOCKED", "WINDOW_CLOSED"])(
    "un 409 %s vuelve a pedir la conversación",
    async (code) => {
      const s = server({ send: () => ({ status: 409, body: { code } }) });
      show();
      await openChat();
      const reads = () =>
        s.api.mock.calls.filter(
          ([url, init]) => String(url).endsWith("/conversations/1/") && init?.method !== "POST",
        ).length;
      await type("Hola");
      const before = reads();
      fireEvent.click(sendButton());
      await screen.findByRole("alert");
      await waitFor(() => expect(reads()).toBeGreaterThan(before));
    },
  );

  it("tras enviar se vuelve a pedir la lista de mensajes (el estado de entrega avanza)", async () => {
    const s = server();
    show();
    await openChat();
    const reads = () =>
      s.api.mock.calls.filter(
        ([url, init]) => String(url).endsWith("/1/messages/") && init?.method !== "POST",
      ).length;
    const before = reads();
    await type("Hola");
    fireEvent.click(sendButton());
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
  });

  it("Enter durante una composición (IME, también keyCode 229 de Safari) no envía", async () => {
    const s = server();
    show();
    await openChat();
    await type("你好");
    fireEvent.keyDown(await box(), { key: "Enter", isComposing: true });
    fireEvent.keyDown(await box(), { key: "Enter", keyCode: 229 });
    await settle();
    expect(s.posts()).toEqual([]);
    fireEvent.keyDown(await box(), { key: "Enter" });
    await waitFor(() => expect(s.posts()).toHaveLength(1));
  });

  it("una conversación cerrada no admite respuestas y lo dice con el texto del servidor", async () => {
    const s = server({ conversation: { status: "CLOSED" } });
    show();
    await openChat();
    expect(await box()).toBeDisabled();
    expect(await screen.findByText(/La conversación está cerrada. Podrás responder/)).toBeVisible();
    const closed = screen.getByText(/La conversación está cerrada. Podrás responder/);
    expect(await box()).toHaveAttribute("aria-describedby", expect.stringContaining(closed.id));
    expect(sendButton()).toBeDisabled();
    await type("Hola");
    fireEvent.keyDown(await box(), { key: "Enter" });
    await settle();
    expect(s.posts()).toEqual([]);
  });

  it("Enter envía, Mayús+Enter no; vacío o solo espacios no se envía", async () => {
    const s = server();
    show();
    await openChat();
    expect(sendButton()).toBeDisabled();
    await type("   \n ");
    expect(sendButton()).toBeDisabled();
    fireEvent.keyDown(await box(), { key: "Enter" });
    await settle();
    expect(s.posts()).toEqual([]);
    await type("Hola");
    fireEvent.keyDown(await box(), { key: "Enter", shiftKey: true });
    await settle();
    expect(s.posts()).toEqual([]);
    fireEvent.keyDown(await box(), { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(s.posts()).toHaveLength(1));
  });

  it("el contador avisa y bloquea al pasar de 4096 caracteres (puntos de código)", async () => {
    const s = server();
    show();
    await openChat();
    await type("😀".repeat(4096));
    expect(screen.getByText("4096 de 4096 caracteres")).toBeVisible();
    expect(sendButton()).toBeEnabled();
    await type("😀".repeat(4097));
    expect(screen.getByText("Te pasaste por 1 carácter.")).toBeVisible();
    expect(sendButton()).toBeDisabled();
    fireEvent.keyDown(await box(), { key: "Enter" });
    await settle();
    expect(s.posts()).toEqual([]);
    expect(await box()).toHaveAttribute("aria-describedby");
  });
});
