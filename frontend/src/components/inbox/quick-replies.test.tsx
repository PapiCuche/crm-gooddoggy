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
import type { Conversation, Grant, QuickReplyUse, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

const BASE = "/api/v1/o/acme/conversations";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
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
const reply = (id: string, shortcut: string, title: string, category: [string, number]) =>
  ({
    ...{ id, shortcut, title, body: `Hola {{contact.first_name}}, ${title.toLowerCase()}.` },
    category: { id: category[0], name: category[0], sort_order: category[1] },
  }) as QuickReplyUse;
// La API las da por creación; el selector las agrupa por el orden de su categoría.
const REPLIES = [
  reply("r1", "delivery", "Envío a domicilio", ["Delivery", 3]),
  reply("r2", "precio", "Lista de precios", ["Precios", 1]),
  reply("r3", "delivery-lima", "Envío en Lima", ["Delivery", 3]),
];

type Reply = { status: number; body?: unknown };
function server(render: () => Reply = () => ({ status: 200, body: rendered })) {
  const api = mockApi({
    [`GET ${BASE}/`]: page([rosa]),
    [`GET ${BASE}/1/`]: { status: 200, body: rosa },
    [`GET ${BASE}/1/messages/`]: page([]),
    [`POST ${BASE}/1/messages/`]: { status: 202, body: { id: "s1", body: "ok" } },
    [`POST ${BASE}/1/quick-replies/r3/render/`]: render,
    [`POST ${BASE}/1/quick-replies/r1/render/`]: render,
  });
  const inner = api.getMockImplementation()!;
  api.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://x");
    if (url.pathname !== `${BASE}/1/quick-replies/`) return inner(input, init);
    const q = url.searchParams.get("q") ?? "";
    const found = REPLIES.filter((one) => `${one.shortcut} ${one.title}`.includes(q));
    return new Response(JSON.stringify({ results: found, next: null }), { status: 200 });
  });
  const calls = (match: (url: URL, method: string) => boolean) =>
    api.mock.calls.filter(([url, init]) =>
      match(new URL(String(url), "http://x"), init?.method ?? "GET"),
    );
  return {
    searches: () =>
      calls((url) => url.pathname.endsWith("/quick-replies/")).map(([url]) =>
        new URL(String(url), "http://x").searchParams.get("q"),
      ),
    renders: () => calls((url) => url.pathname.endsWith("/render/")).length,
    sends: () =>
      calls((url, method) => url.pathname.endsWith("/messages/") && method === "POST").map(
        ([, init]) => JSON.parse(String(init!.body)) as { body: string },
      ),
  };
}
const rendered = {
  ...{ id: "r3", shortcut: "delivery-lima", text: "Hola , el envío en Lima llega hoy." },
  missing: ["contact.first_name"],
};
const show = (permissions = grants("conversations.view", "messages.send", "messages.note")) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  await screen.findByRole("heading", { level: 2 });
};
const box = () => screen.findByRole("textbox", { name: "Responder al cliente" });
const type = async (text: string) => fireEvent.change(await box(), { target: { value: text } });
const key = async (name: string) => fireEvent.keyDown(await box(), { key: name });
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 30)));
const options = () =>
  within(screen.getByRole("listbox", { name: "Respuestas rápidas" })).getAllByRole("option");
const selected = () =>
  within(screen.getByRole("listbox", { name: "Respuestas rápidas" })).getByRole("option", {
    selected: true,
  });

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("respuestas rápidas en el cuadro de respuesta (F6-08b)", () => {
  it("`/` abre la lista agrupada por categoría y `/deli` busca una sola vez, al dejar de teclear", async () => {
    const s = server();
    show();
    await openChat();
    await type("/");
    const list = await screen.findByRole("listbox", { name: "Respuestas rápidas" });
    const groups = await within(list).findAllByRole("group");
    const names = groups.map((group) => {
      const label = document.getElementById(group.getAttribute("aria-labelledby")!);
      return [label?.textContent, within(group).getAllByRole("option").length];
    });
    expect(names).toEqual([
      ["Precios", 1],
      ["Delivery", 2],
    ]);
    // El cuadro sigue siendo el textbox y señala la lista y la opción activa.
    const field = await box();
    expect(field).toHaveAttribute("aria-autocomplete", "list");
    expect(field).toHaveAttribute("aria-controls", list.id);
    expect(field).toHaveAttribute("aria-activedescendant", selected().id);
    for (const text of ["/d", "/de", "/del", "/deli"]) await type(text);
    await waitFor(() => expect(within(list).getAllByRole("option")).toHaveLength(2));
    expect(within(list).queryByText("Lista de precios")).toBeNull();
    expect(s.searches()).not.toEqual(expect.arrayContaining(["d", "de", "del"]));
    expect(s.searches()).toContain("deli");
    // Con un espacio ya es un mensaje: se cierra.
    await type("/deli hoy");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("las flechas mueven la opción, Enter la inserta resuelta sin enviar y avisa lo que faltó", async () => {
    const s = server();
    show();
    await openChat();
    await type("/deli");
    await waitFor(() => expect(options()).toHaveLength(2));
    expect(selected()).toHaveTextContent("/delivery");
    await key("ArrowDown");
    expect(selected()).toHaveTextContent("/delivery-lima");
    await key("ArrowDown"); // vuelve al inicio
    expect(selected()).toHaveTextContent("/delivery");
    await key("ArrowUp");
    expect(selected()).toHaveTextContent("/delivery-lima");
    expect(await box()).toHaveAttribute("aria-activedescendant", selected().id);
    await key("Enter");
    await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(await box()).toHaveFocus();
    expect(screen.getByText(/Quedó sin completar: el nombre del cliente/)).toBeVisible();
    await settle();
    expect(s.renders()).toBe(1);
    expect(s.sends()).toEqual([]); // elegir nunca envía
  });

  it("al hacer clic se inserta; se edita y se envía con el botón de siempre", async () => {
    const s = server();
    show();
    await openChat();
    await type("/delivery-l");
    fireEvent.click(await screen.findByRole("option", { name: /Envío en Lima/ }));
    await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
    expect(s.sends()).toEqual([]);
    await type("Hola Rosa, el envío en Lima llega hoy.");
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    await waitFor(() =>
      expect(s.sends()).toEqual([
        expect.objectContaining({ body: "Hola Rosa, el envío en Lima llega hoy." }),
      ]),
    );
  });

  it("Escape cierra sin tocar el texto; Enter con la lista abierta nunca envía", async () => {
    const s = server(() => ({ status: 404, body: { code: "NOT_FOUND" } }));
    show();
    await openChat();
    await type("/delivery");
    await waitFor(() => expect(options()).toHaveLength(2));
    await key("Enter");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Esa respuesta ya no está disponible",
    );
    expect(await box()).toHaveValue("/delivery");
    await key("Escape");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(await box()).toHaveValue("/delivery");
    await settle();
    expect(s.sends()).toEqual([]);
  });

  it("Enter antes de que llegue la búsqueda espera y elige de lo escrito, no de la lista anterior", async () => {
    const s = server();
    show();
    await openChat();
    await type("/");
    await waitFor(() => expect(options()).toHaveLength(3));
    await type("/delivery-l");
    await key("Enter"); // aún se ve la lista de «/», con «/precio» primero
    await settle();
    expect(s.renders()).toBe(0);
    // Llega la búsqueda de «delivery-l»: se elige su única opción (si fuera «/precio», `render` daría 404).
    await waitFor(() => expect(box()).resolves.toHaveValue(rendered.text));
    expect(s.renders()).toBe(1);
    expect(s.sends()).toEqual([]);
    // Si se sigue escribiendo tras Enter, se olvida.
    await type("/");
    await waitFor(() => expect(options()).toHaveLength(3));
    await type("/pre");
    await key("Enter");
    await type("/prec");
    await waitFor(() => expect(options()).toHaveLength(1));
    await settle();
    expect(s.renders()).toBe(1);
  });

  it("una nota interna no abre el selector", async () => {
    const s = server();
    show(grants("conversations.view", "messages.note"));
    await openChat();
    fireEvent.change(await screen.findByRole("textbox", { name: "Nota interna" }), {
      target: { value: "/delivery" },
    });
    await settle();
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(s.searches()).toEqual([]);
  });
});
