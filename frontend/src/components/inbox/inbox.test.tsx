import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Message, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

type Routes = Parameters<typeof mockApi>[0];

import { Inbox } from "./inbox";

const LIST = "GET /api/v1/o/acme/conversations/";
const DETAIL = (id: string) => `GET /api/v1/o/acme/conversations/${id}/`;
const MESSAGES = (id: string, query = "") =>
  `GET /api/v1/o/acme/conversations/${id}/messages/${query}`;
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "conversations.view", scopes: [] }],
};
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const conversation = (id: string, changes: Partial<Conversation> = {}) =>
  ({
    ...{ id, number: `CONV-0000${id}`, status: "OPEN", attention_mode: "HUMAN" },
    ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
    ...{ contact: { id: `c${id}`, display_name: "Rosa Díaz" } },
    ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" } },
    ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: "2026-11-04T15:30:00Z" },
    ...{ last_message_preview: "Hola, ¿tienen collares?", last_message_direction: "INBOUND" },
    ...{ version: 1, ...changes },
  }) as Conversation;
const message = (id: string, changes: Partial<Message> = {}) =>
  ({
    ...{ id, direction: "INBOUND", sender_type: "CONTACT", sender_user_id: null },
    ...{ message_type: "TEXT", body: `texto ${id}`, client_message_id: null },
    ...{ delivery_status: null, created_at: "2026-11-04T15:30:00Z", sent_at: null },
    ...{ delivered_at: null, read_at: null, failed_at: null, ...changes },
  }) as Message;
const rosa = conversation("1");
// Cada fila de la lista tiene su detalle, salvo que el test diga otra cosa.
const serve = (routes: Routes) => {
  const reply = routes[LIST];
  const rows =
    typeof reply === "object" ? ((reply.body as { results?: Conversation[] })?.results ?? []) : [];
  return mockApi({
    ...Object.fromEntries(rows.map((row) => [DETAIL(row.id), { status: 200, body: row }])),
    ...routes,
  });
};
const show = (codes = ["conversations.view"]) =>
  renderApp(
    <TenantProvider value={{ ...tenant, permissions: codes.map((code) => ({ code, scopes: [] })) }}>
      <Inbox />
    </TenantProvider>,
  );
const open = async (name: string | RegExp) => {
  fireEvent.click(await screen.findByRole("button", { name }));
  return screen.findByRole("heading", { level: 2 });
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Inbox, lista (F6-03)", () => {
  it("carga, muestra cada conversación con su vista previa y marca la elegida", async () => {
    const luna = conversation("2", {
      ...{ contact: { id: "c2", display_name: "Luis Ramos" }, assigned_user_id: "u1" },
      ...{ assignee_kind: "USER", last_message_preview: "Sí, hay", status: "PENDING" },
      ...{ last_message_direction: "OUTBOUND" },
    });
    serve({ [LIST]: page([rosa, luna]), [MESSAGES("1")]: page([]) });
    show();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando conversaciones…");
    const row = await screen.findByRole("button", { name: /Rosa Díaz/ });
    expect(row).toHaveTextContent("Hola, ¿tienen collares?");
    expect(row).toHaveTextContent("CONV-00001SandboxAbiertaSin asignar");
    expect(screen.getByRole("button", { name: /Luis Ramos/ })).toHaveTextContent(
      "Tú: Sí, hayCONV-00002SandboxPendienteAsignada a ti",
    );
    expect(row).not.toHaveAttribute("aria-current");
    await open(/Rosa Díaz/);
    expect(row).toHaveAttribute("aria-current", "true");
  });

  it("vacío, fallo con reintento y 403 sin reintento", async () => {
    let reply: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    serve({ [LIST]: () => reply });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    reply = page([]);
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("Todavía no hay conversaciones.")).toBeVisible();
    cleanup();
    serve({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver las conversaciones de esta organización.",
    );
    expect(screen.queryByRole("button", { name: "Reintentar" })).toBeNull();
  });

  it("la bandeja y el estado son filtros de la API y se combinan", async () => {
    const api = serve({
      [LIST]: page([rosa]),
      [`${LIST}?box=mine`]: page([]),
      [`${LIST}?box=mine&status=OPEN`]: page([conversation("3")]),
    });
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    expect(screen.getByRole("button", { name: "Todas" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Mías" }));
    expect(await screen.findByText("Ninguna conversación coincide con los filtros.")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Estado"), { target: { value: "OPEN" } });
    expect(await screen.findByText("CONV-00003")).toBeVisible();
    expect(screen.getByRole("button", { name: "Mías" })).toHaveAttribute("aria-pressed", "true");
    expect(api.mock.calls.map(([url]) => String(url))).toContain(
      "/api/v1/o/acme/conversations/?box=mine&status=OPEN",
    );
  });

  it("«Cargar más» pide la página siguiente y, si falla, no borra lo ya mostrado", async () => {
    let next: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    serve({
      [LIST]: page([rosa], "c1"),
      [`${LIST}?cursor=c1`]: () => next,
    });
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(screen.getByRole("button", { name: /Rosa Díaz/ })).toBeVisible(); // sigue ahí
    next = page([conversation("4", { contact: { id: "c4", display_name: "Marta Paz" } })]);
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByRole("button", { name: /Marta Paz/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Rosa Díaz/ })).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cargar más" })).toBeNull();
  });

  it("sin permiso sobre el contacto explica en vez de dejar un hueco", async () => {
    const hidden = conversation("5", {
      ...{ contact: { id: "c5", display_name: null }, last_message_preview: null },
    });
    serve({ [LIST]: page([hidden]), [MESSAGES("5")]: page([message("m1", { body: null })]) });
    show();
    const row = await screen.findByRole("button", { name: /Cliente no disponible/ });
    expect(row).toHaveTextContent("Sin permiso para ver el contenido");
    await open(/Cliente no disponible/);
    expect(
      await screen.findByText("Sin permiso para ver el contenido de este mensaje."),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Cliente y oportunidades" })).toBeNull(); // sin nombre no hay panel
    expect(screen.getByText(/No tienes permiso para ver a este cliente/)).toBeVisible();
    cleanup();
    // aunque el servidor mandara la vista previa sin el nombre, la pantalla no la pinta
    const leaky = conversation("7", { contact: { id: "c7", display_name: null } });
    serve({ [LIST]: page([leaky]) });
    show();
    const hiddenRow = await screen.findByRole("button", { name: /Cliente no disponible/ });
    expect(hiddenRow).not.toHaveTextContent("Hola, ¿tienen collares?");
  });
});

describe("Inbox, chat (F6-03)", () => {
  it("pinta los mensajes del más antiguo al más reciente con marcas de entrega, notas rotuladas y la ficha", async () => {
    const sent = message("m3", {
      direction: "OUTBOUND",
      sender_type: "USER",
      delivery_status: "READ",
    });
    serve({
      [LIST]: page([rosa]),
      [MESSAGES("1")]: page([
        sent,
        message("m2", {
          direction: "INTERNAL",
          sender_type: "USER",
          message_type: "NOTE",
          body: "Pide DNI",
        }),
        message("m1", { body: "Hola", delivery_status: "DELIVERED" }),
        message("m0", {
          direction: "OUTBOUND",
          sender_type: "USER",
          delivery_status: "FAILED",
          body: "Falló",
        }),
      ]),
    });
    show();
    const heading = await open(/Rosa Díaz/);
    await waitFor(() => expect(heading).toHaveFocus()); // en 390 px la lista se oculta: el foco va al chat
    // el título de la página sigue en el árbol de accesibilidad aunque se vea solo el chat
    expect(screen.getByRole("heading", { level: 1, name: "Inbox" })).toHaveClass("max-lg:sr-only");
    const log = await screen.findByRole("list", { name: "Mensajes" });
    // 390 y 320 px: los controles se reparten en líneas en vez de salirse del recuadro (jsdom no
    // mide; la medición real es en el navegador)
    const refresh = screen.getByRole("button", { name: "Actualizar mensajes" });
    expect(refresh.parentElement).toHaveClass("flex", "flex-wrap");
    expect(screen.getByRole("button", { name: "Mías" }).parentElement).toHaveClass("flex-wrap");
    // encabezado de una fila: volver, nombre, ficha y actualizar; el número y el estado debajo
    const card = screen.getByRole("button", { name: "Cliente y oportunidades" });
    expect(card.parentElement).toBe(refresh.parentElement);
    // Tab sigue el orden visual: el DOM es [volver, actualizar, ficha] y `order` coloca cada pieza
    // (en escritorio actualizar sube a la derecha y la ficha baja al final de la fila)
    const back = screen.getByRole("button", { name: "Volver a las conversaciones" });
    expect(back).toHaveClass("order-1", "lg:hidden"); // en escritorio no hay «Volver»
    expect(back.compareDocumentPosition(refresh) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(refresh.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(refresh).toHaveClass("order-3", "lg:order-2", "lg:ml-auto");
    // 40 px de lado en móvil (el tamaño táctil que paga el alto mínimo del panel)
    expect(back).toHaveClass("size-10");
    expect(refresh).toHaveClass("max-lg:size-10");
    expect(card).toHaveClass("max-sm:size-10");
    expect(card).toHaveClass("order-4", "lg:order-6"); // icono en 320 px, al final en escritorio
    // los botones de icono solo lo son en pantallas estrechas; en escritorio dicen su texto
    expect(within(refresh).getByText("Actualizar mensajes")).toHaveClass("max-lg:sr-only");
    expect(within(card).getByText("Cliente y oportunidades")).toHaveClass("max-sm:sr-only");
    expect(within(card.closest("header")!).getByText("CONV-00001").parentElement).toHaveClass(
      "order-5",
      "basis-full",
    );
    // la región que se desplaza se recorre con el teclado
    const scroller = screen.getByRole("region", { name: "Mensajes de la conversación" });
    expect(scroller).toHaveAttribute("tabindex", "0");
    expect(scroller).toContainElement(log);
    const items = within(log).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector("bdi")?.textContent)).toEqual([
      "Falló",
      "Hola",
      "Pide DNI",
      "texto m3",
    ]);
    expect(items[0]).toHaveTextContent("No se pudo enviar");
    expect(items[3]).toHaveTextContent("Leído");
    expect(items[1]).not.toHaveTextContent("Entregado"); // lo entrante no lleva marca
    expect(items[2]).toHaveTextContent("Nota interna · solo la ve tu equipo");
    expect(items[2]).toHaveClass("border-dashed");
    expect(items[1]).not.toHaveTextContent("Nota interna");
    fireEvent.click(screen.getByRole("button", { name: "Cliente y oportunidades" }));
    expect(await screen.findByRole("link", { name: "Ver ficha del cliente" })).toHaveAttribute(
      "href",
      "/o/acme/clientes/c1",
    );
  });

  it("los mensajes anteriores se cargan aparte y un fallo no borra el chat", async () => {
    let older: { status: number; body?: unknown } = {
      status: 500,
      body: { code: "INTERNAL_ERROR" },
    };
    serve({
      [LIST]: page([rosa]),
      [MESSAGES("1")]: page([message("m2", { body: "Reciente" })], "m2"),
      [MESSAGES("1", "?cursor=m2")]: () => older,
    });
    show();
    await open(/Rosa Díaz/);
    await screen.findByText("Reciente");
    fireEvent.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(screen.getByText("Reciente")).toBeVisible();
    older = page([message("m1", { body: "Antiguo" })]);
    fireEvent.click(screen.getByRole("button", { name: "Cargar mensajes anteriores" }));
    await screen.findByText("Antiguo");
    const texts = within(screen.getByRole("list", { name: "Mensajes" }))
      .getAllByRole("listitem")
      .map((li) => li.querySelector("bdi")?.textContent);
    expect(texts).toEqual(["Antiguo", "Reciente"]);
    expect(screen.queryByRole("button", { name: "Cargar mensajes anteriores" })).toBeNull();
  });

  it("el encabezado se alimenta del detalle, no de la instantánea de la lista", async () => {
    serve({
      [LIST]: page([rosa]),
      [DETAIL("1")]: { status: 200, body: { ...rosa, status: "RESOLVED" } },
      [MESSAGES("1")]: page([]),
    });
    show();
    await open(/Rosa Díaz/);
    const chat = within(screen.getByRole("region", { name: "Conversación" }));
    expect(await chat.findByText("Resuelta")).toBeVisible(); // la lista decía «Abierta»
    expect(chat.queryByText("Abierta")).toBeNull();
  });

  it("si el detalle falla, el chat sigue con la fila y lo avisa", async () => {
    serve({
      [LIST]: page([rosa]),
      [DETAIL("1")]: { status: 500, body: { code: "INTERNAL_ERROR" } },
      [MESSAGES("1")]: page([message("m1", { body: "Hola" })]),
    });
    show();
    const heading = await open(/Rosa Díaz/);
    expect(await screen.findByText("Hola")).toBeVisible();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(heading).toHaveTextContent("Rosa Díaz");
  });

  it("vacío y 404 del chat se explican; «Volver» devuelve el foco a la fila", async () => {
    serve({
      [LIST]: page([rosa]),
      [MESSAGES("1")]: { status: 404, body: { code: "NOT_FOUND" } },
    });
    show();
    await open(/Rosa Díaz/);
    expect(await screen.findByText("No encontramos lo que buscas.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Volver a las conversaciones" }));
    await waitFor(() => expect(document.getElementById("conversation-1")).toHaveFocus());
    expect(screen.queryByRole("heading", { level: 2 })).toBeNull();
  });

  it("al cambiar de conversación nunca se ven los mensajes de la anterior", async () => {
    const luis = conversation("2", { contact: { id: "c2", display_name: "Luis Ramos" } });
    serve({
      [LIST]: page([rosa, luis]),
      [MESSAGES("1")]: page([message("m1", { body: "de Rosa" })]),
      [MESSAGES("2")]: page([message("m2", { body: "de Luis" })]),
    });
    show();
    await open(/Rosa Díaz/);
    await screen.findByText("de Rosa");
    fireEvent.click(screen.getByRole("button", { name: /Luis Ramos/ }));
    expect(screen.queryByText("de Rosa")).toBeNull(); // ni un instante, ni de la caché
    expect(await screen.findByText("de Luis")).toBeVisible();
    expect(screen.queryByText("de Rosa")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Cliente y oportunidades" }));
    expect(await screen.findByRole("link", { name: "Ver ficha del cliente" })).toHaveAttribute(
      "href",
      "/o/acme/clientes/c2",
    );
  });

  it("el texto hostil se pinta como texto, aislado y sin ejecutarse", async () => {
    const hostile = "‮gnp.exe <img src=x onerror=alert(1)> <b>negrita</b>​\nlínea 2";
    const evil = conversation("6", {
      ...{ contact: { id: "c6", display_name: "<script>x</script>‮" } },
      ...{ last_message_preview: hostile },
    });
    serve({ [LIST]: page([evil]), [MESSAGES("6")]: page([message("m1", { body: hostile })]) });
    const { container } = show();
    const row = await screen.findByRole("button", { name: /script/ });
    expect(row).toHaveTextContent(hostile.replace("\n", " "));
    const previews = Array.from(row.querySelectorAll("bdi"));
    expect(previews.map((bdi) => bdi.textContent)).toEqual([
      "<script>x</script>\u202E", // el nombre del cliente también va aislado
      hostile, // tal cual, sin interpretar
      "Sandbox", // y el nombre del canal
    ]);
    for (const bdi of previews) {
      expect(bdi).toHaveAttribute("dir", "auto");
      expect(bdi).toHaveClass("whitespace-pre-wrap", "wrap-anywhere");
    }
    const heading = await open(/script/);
    expect(heading.querySelector("bdi")).toHaveAttribute("dir", "auto");
    const header = screen.getByRole("region", { name: "Conversación" }).querySelector("header");
    expect(Array.from(header!.querySelectorAll("bdi")).map((b) => b.textContent)).toEqual([
      "<script>x</script>\u202E",
      "Sandbox",
    ]);
    const body = await screen.findByRole("list", { name: "Mensajes" });
    const bdi = body.querySelector("bdi")!;
    expect(bdi.textContent).toBe(hostile); // tal cual, sin interpretar
    expect(bdi).toHaveAttribute("dir", "auto");
    expect(bdi).toHaveClass("whitespace-pre-wrap", "wrap-anywhere");
    expect(container.querySelector("img, script, b")).toBeNull();
  });
});
