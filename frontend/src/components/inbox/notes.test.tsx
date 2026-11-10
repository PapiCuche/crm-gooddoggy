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
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [],
};
const grant = (code: string, scope: "ORGANIZATION" | "OWN" = "ORGANIZATION"): Grant => ({
  ...{ code, scopes: [scope] },
});
const BOTH = [grant("conversations.view"), grant("messages.send"), grant("messages.note")];
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" }, assignment_version: 1, version: 1 },
  ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" }, last_message_preview: null },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null },
  ...{ last_message_direction: null },
} as unknown as Conversation;
const note = (id: string, body: string | null, changes: Partial<Message> = {}) =>
  ({
    ...{ id, direction: "INTERNAL", sender_type: "USER", sender_user_id: "u1" },
    ...{ message_type: "NOTE", body, client_message_id: `cm-${id}`, delivery_status: null },
    ...{ created_at: "2026-11-04T16:00:00Z", sent_at: null, delivered_at: null },
    ...{ read_at: null, failed_at: null, ...changes },
  }) as Message;

type Reply = { status: number; body?: unknown };
// El servidor: una nota guardada se lista (con su texto o sin él, según quien la ve).
function server(
  options: { conversation?: Partial<Conversation>; hide?: boolean; notes?: () => Reply } = {},
) {
  const conversation = { ...rosa, ...options.conversation };
  const stored: Message[] = [];
  const api = mockApi({
    [`GET ${BASE}/`]: { status: 200, body: { results: [conversation], next: null } },
    [`GET ${BASE}/1/`]: { status: 200, body: conversation },
    [`GET ${BASE}/1/messages/`]: () => ({
      status: 200,
      body: { results: [...stored].reverse(), next: null },
    }),
    [`POST ${BASE}/1/messages/`]: {
      status: 202,
      body: { ...note("m1", "x"), direction: "OUTBOUND" },
    },
    [`POST ${BASE}/1/notes/`]: () => {
      const reply = options.notes?.() ?? {
        status: 201,
        body: note("n1", options.hide ? null : "x"),
      };
      if (reply.status < 300) stored.push(reply.body as Message);
      return reply;
    },
  });
  let release = () => {};
  let hold: Promise<void> | null = null;
  const inner = api.getMockImplementation()!;
  api.mockImplementation(async (input, init) => {
    if (init?.method === "POST") await hold;
    return inner(input, init);
  });
  const posts = (route: "messages" | "notes") =>
    api.mock.calls
      .filter(([url, init]) => String(url).endsWith(`/1/${route}/`) && init?.method === "POST")
      .map(
        ([, init]) => JSON.parse(String(init!.body)) as { client_message_id: string; body: string },
      );
  return {
    api,
    posts,
    wait: () => void (hold = new Promise<void>((resolve) => (release = resolve))),
    go: () => release(),
  };
}
const show = (permissions: Grant[] = BOTH) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions }}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  await screen.findByRole("heading", { level: 2 });
};
const reply = () => screen.findByRole("textbox", { name: "Responder al cliente" });
const noteBox = () => screen.findByRole("textbox", { name: "Nota interna" });
const mode = (name: string) => screen.getByRole("button", { name });
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 30)));

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

describe("notas internas en el cuadro de redacción (F6-05c)", () => {
  it("con solo `messages.send` hay un cuadro de respuesta y ningún conmutador", async () => {
    server();
    show([grant("conversations.view"), grant("messages.send")]);
    await openChat();
    expect(await reply()).toBeVisible();
    expect(screen.queryByRole("button", { name: "Nota interna" })).toBeNull();
  });

  it("con solo `messages.note` hay un cuadro de nota («Guardar nota», nunca «Enviar») y ningún conmutador", async () => {
    server();
    show([grant("conversations.view"), grant("messages.note")]);
    await openChat();
    expect(await noteBox()).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Responder al cliente" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Responder al cliente" })).toBeNull();
    expect(await screen.findByRole("button", { name: "Guardar nota" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Enviar" })).toBeNull();
  });

  it.each([
    ["sin ninguno de los dos permisos", {}, [grant("conversations.view")]],
    // con alcance OWN, solo en una conversación propia
    [
      "con `messages.note` de alcance OWN en una conversación ajena",
      { assigned_user_id: "u2" },
      [grant("conversations.view"), grant("messages.note", "OWN")],
    ],
  ])("no hay cuadro %s", async (_name, conversation, permissions) => {
    server({ conversation });
    show(permissions);
    await openChat();
    expect(
      await screen.findByText(/No tienes permiso para responder ni dejar notas/),
    ).toBeVisible();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("si deja de poder responder (ya no es suya), el modo elegido cae al que sí puede y no queda vacío", async () => {
    const s = server({ conversation: { assigned_user_id: "u1", assignee_kind: "USER" } });
    const inner = s.api.getMockImplementation()!;
    let moved = false;
    s.api.mockImplementation(async (input, init) =>
      moved && String(input) === `${BASE}/1/`
        ? new Response(JSON.stringify({ ...rosa, assigned_user_id: "u2", assignee_kind: "USER" }))
        : inner(input, init),
    );
    show([grant("conversations.view"), grant("messages.send", "OWN"), grant("messages.note")]);
    await openChat();
    expect(await reply()).toBeVisible(); // suya: puede responder y anotar
    moved = true; // otra persona la tiene ahora: la respuesta (OWN) ya no, la nota (toda la org.) sí
    fireEvent.click(screen.getByRole("button", { name: "Actualizar mensajes" }));
    expect(await noteBox()).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Responder al cliente" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Responder al cliente" })).toBeNull(); // sin conmutador
    expect(screen.getByRole("button", { name: "Guardar nota" })).toBeVisible();
  });

  it("arranca siempre en «Responder» y el modo nota se distingue por texto, borde y botón", async () => {
    server();
    show();
    await openChat();
    expect(mode("Responder al cliente")).toHaveAttribute("aria-pressed", "true");
    expect(mode("Nota interna")).toHaveAttribute("aria-pressed", "false");
    expect(await reply()).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Nota interna" })).toBeNull(); // oculta, no solo atenuada
    expect(screen.getByRole("button", { name: "Enviar" })).toBeVisible();
    fireEvent.click(mode("Nota interna"));
    expect(mode("Nota interna")).toHaveAttribute("aria-pressed", "true");
    const box = await noteBox();
    expect(box).toHaveClass("border-dashed");
    expect(box.closest("form")).toHaveClass("border-dashed");
    expect(screen.getByText(/Solo tu equipo ve esto, el cliente no/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Guardar nota" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Enviar" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Responder al cliente" })).toBeNull();
  });

  it("el cuadro es compacto en 390 px: una línea que crece, etiqueta solo para lectores con conmutador, contador discreto", async () => {
    server();
    show();
    await openChat();
    const box = await reply();
    expect(box).toHaveAttribute("rows", "1");
    expect(box).toHaveClass("field-sizing-content", "lg:field-sizing-fixed", "max-h-24"); // crece hasta un máximo
    expect(screen.getByText("Responder al cliente", { selector: "label" })).toHaveClass(
      "max-lg:sr-only",
    );
    const form = within(box.closest("form")!);
    const count = form.getByText("0 de 4096 caracteres");
    expect(count).toHaveClass("max-lg:sr-only"); // lejos del tope no ocupa una línea
    fireEvent.change(box, { target: { value: "a".repeat(4000) } });
    expect(form.getByText("4000 de 4096 caracteres")).not.toHaveClass("max-lg:sr-only"); // cerca del tope sí
    cleanup();
    server();
    show([grant("conversations.view"), grant("messages.send")]); // sin conmutador la etiqueta se ve
    await openChat();
    expect(screen.getByText("Responder al cliente", { selector: "label" })).not.toHaveClass(
      "max-lg:sr-only",
    );
  }, 20_000);

  it("en 320 px el conmutador cabe en una fila con etiquetas cortas y el nombre completo, y el botón no gasta una línea", async () => {
    server();
    show();
    await openChat();
    const row = mode("Responder al cliente").parentElement!;
    expect(row).toHaveClass("flex");
    expect(row).not.toHaveClass("flex-wrap"); // una sola fila
    for (const [name, short] of [
      ["Responder al cliente", "Responder"],
      ["Nota interna", "Nota"],
    ]) {
      const button = mode(name!);
      expect(button).toHaveClass("max-sm:flex-1");
      expect(button).toHaveAttribute("aria-label", name); // el nombre accesible no se acorta
      expect(within(button).getByText(short!)).toHaveClass("sm:hidden"); // corta, solo en pantallas estrechas
      expect(within(button).getByText(name!)).toHaveClass("max-sm:hidden");
    }
    // respuesta: el cuadro y un botón de icono en la misma fila; el texto del botón queda para lectores
    const form = (await reply()).closest("form")!;
    expect(form).toHaveClass("max-sm:grid", "max-sm:grid-cols-[minmax(0,1fr)_auto]");
    const send = within(form).getByRole("button", { name: "Enviar" });
    expect(send).toHaveClass("max-sm:size-10", "max-sm:col-start-2", "max-sm:row-start-1");
    expect(within(send).getByText("Enviar")).toHaveClass("max-sm:sr-only");
    // nota: el cuadro ocupa la fila y debajo van el aviso y «Guardar nota», visible y distinto
    fireEvent.click(mode("Nota interna"));
    const noteForm = (await noteBox()).closest("form")!;
    expect(await noteBox()).toHaveClass("max-sm:col-span-2");
    const save = within(noteForm).getByRole("button", { name: "Guardar nota" });
    expect(save).toHaveClass("max-sm:row-start-2", "max-sm:col-start-2");
    expect(within(save).getByText("Guardar nota")).not.toHaveClass("max-sm:sr-only");
    expect(within(noteForm).getByText(/Solo tu equipo ve esto, el cliente no/)).not.toHaveClass(
      "max-lg:sr-only",
    );
  }, 20_000);

  it("el panel reparte el alto: encabezado y cuadro fijos, solo los mensajes se desplazan", async () => {
    server();
    show();
    await openChat();
    const panel = screen.getByRole("region", { name: "Conversación" }).firstElementChild!;
    expect(panel).toHaveClass(
      "h-[calc(100dvh-8rem)]",
      "min-h-[29rem]",
      "flex-col",
      "lg:h-[calc(100dvh-18rem)]",
    );
    const scroller = screen.getByRole("region", { name: "Mensajes de la conversación" });
    expect(scroller).toHaveAttribute("tabindex", "0"); // sigue enfocable con el teclado
    expect(scroller).toHaveClass("min-h-0", "flex-1", "overflow-y-auto"); // la válvula: absorbe el sobrante y nunca empuja el cuadro fuera
    expect(panel.querySelector("header")).toHaveClass("shrink-0");
    expect(scroller.nextElementSibling).toHaveClass("shrink-0"); // el cuadro nunca se comprime ni sale
  });

  it("la descripción del cuadro apunta a elementos que existen: contador, teclas y, en la nota, el aviso", async () => {
    server();
    show();
    await openChat();
    const described = (box: HTMLElement) =>
      box
        .getAttribute("aria-describedby")!
        .split(" ")
        .map((id) => document.getElementById(id)?.textContent);
    expect(described(await reply())).toEqual([
      "0 de 4096 caracteres",
      "Enter envía; Mayús+Enter agrega una línea.",
    ]);
    fireEvent.click(mode("Nota interna"));
    expect(described(await noteBox())).toEqual([
      "0 de 4096 caracteres",
      "Enter guarda; Mayús+Enter agrega una línea.",
      "Solo tu equipo ve esto, el cliente no.",
    ]);
    expect(screen.getByText("Solo tu equipo ve esto, el cliente no.")).not.toHaveClass("sr-only");
  }, 20_000);

  it("guardar una nota usa solo `notes/`, la muestra rotulada y sin marca de entrega, y enfoca el cuadro", async () => {
    const s = server({ notes: () => ({ status: 201, body: note("n1", "Pide DNI") }) });
    show();
    await openChat();
    fireEvent.click(mode("Nota interna"));
    fireEvent.change(await noteBox(), { target: { value: "Pide DNI" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar nota" }));
    const log = await screen.findByRole("list", { name: "Mensajes" });
    expect(await within(log).findByText("Nota interna · solo la ve tu equipo")).toBeVisible();
    expect(log).toHaveTextContent("Pide DNI");
    expect(log).not.toHaveTextContent("En cola"); // una nota no se encola ni se entrega
    await waitFor(() => expect(noteBox()).resolves.toHaveValue(""));
    expect(await noteBox()).toHaveFocus();
    expect(await screen.findByText("Nota guardada.")).toBeVisible();
    expect(s.posts("notes")).toEqual([
      { client_message_id: expect.stringMatching(/^[0-9a-f-]{36}$/), body: "Pide DNI" },
    ]);
    expect(s.posts("messages")).toEqual([]); // nunca por la ruta de los mensajes al cliente
  });

  it("mientras se guarda, la nota ya se ve rotulada y nunca «En cola»", async () => {
    const s = server();
    show();
    await openChat();
    fireEvent.click(mode("Nota interna"));
    s.wait();
    fireEvent.change(await noteBox(), { target: { value: "Pide DNI" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar nota" }));
    const log = await screen.findByRole("list", { name: "Mensajes" });
    expect(await within(log).findByText("Nota interna · solo la ve tu equipo")).toBeVisible();
    expect(log).toHaveTextContent("Pide DNI");
    expect(log).not.toHaveTextContent("En cola");
    expect(screen.getByRole("button", { name: "Guardando…" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    s.go();
    expect(await screen.findByText("Nota guardada.")).toBeVisible();
  });

  it("responder nunca llama a `notes/`, y el mismo texto lleva un id nuevo en cada modo y acción", async () => {
    const s = server();
    show();
    await openChat();
    fireEvent.change(await reply(), { target: { value: "igual" } });
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    await waitFor(() => expect(s.posts("messages")).toHaveLength(1));
    fireEvent.click(mode("Nota interna"));
    for (let times = 0; times < 2; times++) {
      fireEvent.change(await noteBox(), { target: { value: "igual" } });
      fireEvent.click(screen.getByRole("button", { name: "Guardar nota" }));
      await waitFor(() => expect(s.posts("notes")).toHaveLength(times + 1));
      await waitFor(() => expect(noteBox()).resolves.toHaveValue(""));
    }
    expect(s.posts("messages")).toHaveLength(1);
    const ids = [...s.posts("messages"), ...s.posts("notes")].map((post) => post.client_message_id);
    expect(new Set(ids).size).toBe(3); // ninguno se repite: mensaje y nota comparten el espacio
  });

  it("cada modo conserva su borrador al cambiar", async () => {
    server();
    show();
    await openChat();
    fireEvent.change(await reply(), { target: { value: "borrador de respuesta" } });
    fireEvent.click(mode("Nota interna"));
    fireEvent.change(await noteBox(), { target: { value: "borrador de nota" } });
    fireEvent.click(mode("Responder al cliente"));
    expect(await reply()).toHaveValue("borrador de respuesta");
    fireEvent.click(mode("Nota interna"));
    expect(await noteBox()).toHaveValue("borrador de nota");
  });

  it("un fallo conserva el texto y «Reintentar» reutiliza el id; 403/404 y un id ocupado se explican", async () => {
    let answer: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    const s = server({ notes: () => answer });
    show();
    await openChat();
    fireEvent.click(mode("Nota interna"));
    fireEvent.change(await noteBox(), { target: { value: "Pide DNI" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar nota" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(await noteBox()).toHaveValue("Pide DNI");
    // el aviso va tras el contador y el botón, en el DOM y en la rejilla de 320 px, y el botón cambia de icono
    const form = (await noteBox()).closest("form")!;
    const alert = within(form).getByRole("alert");
    const retry = within(form).getByRole("button", { name: "Reintentar" });
    expect(alert).toHaveClass("max-sm:col-span-2");
    expect(retry.compareDocumentPosition(alert) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(retry.querySelector("svg")).toHaveClass("lucide-sticky-note"); // la nota conserva su icono
    expect(retry.parentElement).toHaveClass("max-sm:contents"); // hint y botón son celdas de la rejilla
    answer = { status: 404, body: { code: "NOT_FOUND" } };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "No puedes dejar notas en esta conversación",
      ),
    );
    answer = { status: 409, body: { code: "CLIENT_MESSAGE_ID_TAKEN" } };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Ese id de mensaje ya se usó"),
    );
    answer = { status: 201, body: note("n1", "Pide DNI") };
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    await waitFor(() => expect(s.posts("notes")).toHaveLength(4));
    const [a, b, c, d] = s.posts("notes").map((post) => post.client_message_id);
    expect([b, c]).toEqual([a, a]);
    expect(d).not.toBe(c); // tras CLIENT_MESSAGE_ID_TAKEN
  }, 20_000);

  it("quien anota sin ver al contacto recibe el texto vacío: se explica", async () => {
    server({ hide: true, notes: () => ({ status: 201, body: note("n1", null) }) });
    show();
    await openChat();
    fireEvent.click(mode("Nota interna"));
    fireEvent.change(await noteBox(), { target: { value: "Pide DNI" } });
    fireEvent.click(screen.getByRole("button", { name: "Guardar nota" }));
    expect(await screen.findByText(/Nota guardada. No puedes ver a este cliente/)).toBeVisible();
    expect(
      await screen.findByText("Sin permiso para ver el contenido de este mensaje."),
    ).toBeVisible();
  });

  it("en una conversación cerrada no se responde, pero sí se anota", async () => {
    const s = server({ conversation: { status: "CLOSED" } });
    show();
    await openChat();
    expect(await reply()).toBeDisabled();
    expect(screen.getByText(/La conversación está cerrada. Podrás responder/)).toBeVisible();
    fireEvent.click(mode("Nota interna"));
    expect(await noteBox()).toBeEnabled();
    fireEvent.change(await noteBox(), { target: { value: "Seguimiento" } });
    fireEvent.keyDown(await noteBox(), { key: "Enter" });
    await waitFor(() => expect(s.posts("notes")).toHaveLength(1));
    expect(s.posts("messages")).toEqual([]);
  });

  it("al fallar una respuesta, el botón pasa a «Reintentar» con su icono y los avisos van tras el contador", async () => {
    const s = server();
    const inner = s.api.getMockImplementation()!;
    s.api.mockImplementation(async (input, init) =>
      init?.method === "POST"
        ? new Response(JSON.stringify({ code: "INTERNAL_ERROR" }), { status: 500 })
        : inner(input, init),
    );
    show([grant("conversations.view"), grant("messages.send")]);
    await openChat();
    fireEvent.change(await reply(), { target: { value: "Hola" } });
    fireEvent.click(screen.getByRole("button", { name: "Enviar" }));
    const retry = await screen.findByRole("button", { name: "Reintentar" });
    expect(retry.querySelector("svg")).toHaveClass("lucide-rotate-cw");
    expect(retry.querySelector("svg")).not.toHaveClass("lucide-send");
    const alert = screen.getByRole("alert");
    expect(alert).toHaveClass("max-sm:col-span-2");
    expect(retry.compareDocumentPosition(alert) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  }, 20_000);

  it("el tope se anuncia con aria-live solo al cruzar el 90 % y el 100 %", async () => {
    server();
    show([grant("conversations.view"), grant("messages.send")]);
    await openChat();
    const box = await reply();
    const live = () =>
      Array.from(document.querySelectorAll("[aria-live=polite]")).map((one) => one.textContent);
    expect(live()).toEqual([""]); // lejos del tope no dice nada
    fireEvent.change(box, { target: { value: "a".repeat(100) } });
    fireEvent.change(box, { target: { value: "a".repeat(3000) } });
    expect(live()).toEqual([""]);
    fireEvent.change(box, { target: { value: "a".repeat(3700) } });
    expect(live()).toEqual(["Te acercas al tope de caracteres."]);
    fireEvent.change(box, { target: { value: "a".repeat(3900) } });
    expect(live()).toEqual(["Te acercas al tope de caracteres."]); // el mismo texto: no se vuelve a anunciar
    fireEvent.change(box, { target: { value: "a".repeat(4097) } });
    expect(live()).toEqual(["Pasaste el tope de caracteres: no se puede enviar."]);
    fireEvent.change(box, { target: { value: "a".repeat(10) } });
    expect(live()).toEqual([""]);
    // en el modo nota el aviso habla de guardar, no de enviar
    cleanup();
    show([grant("conversations.view"), grant("messages.note")]);
    await openChat();
    fireEvent.change(await noteBox(), { target: { value: "a".repeat(4097) } });
    expect(live()).toEqual(["Pasaste el tope de caracteres: no se puede guardar."]);
  }, 20_000);

  it("vacío, solo espacios y pasarse del tope no guardan; Enter durante un IME tampoco", async () => {
    const s = server();
    show([grant("conversations.view"), grant("messages.note")]);
    await openChat();
    expect(screen.getByRole("button", { name: "Guardar nota" })).toBeDisabled();
    fireEvent.change(await noteBox(), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Guardar nota" })).toBeDisabled();
    fireEvent.change(await noteBox(), { target: { value: "a".repeat(4097) } });
    expect(screen.getByText("Te pasaste por 1 carácter.")).toBeVisible();
    fireEvent.keyDown(await noteBox(), { key: "Enter" });
    fireEvent.change(await noteBox(), { target: { value: "你好" } });
    fireEvent.keyDown(await noteBox(), { key: "Enter", keyCode: 229 });
    await settle();
    expect(s.posts("notes")).toEqual([]);
  });
});
