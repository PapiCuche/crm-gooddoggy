import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, Message, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

// F7-09b3: adjuntar una imagen o un PDF desde el cuadro de redacción (`POST …/media/`, F7-09b2).
const BASE = "/api/v1/o/acme/conversations";
const MEDIA = `POST ${BASE}/1/media/`;
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: ["conversations.view", "messages.send"].map((code) => ({
    code,
    scopes: ["ORGANIZATION" as const],
  })),
};
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" } },
  ...{ channel: { id: "ch1", platform: "SANDBOX", name: "Sandbox" } },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: null, version: 1 },
  ...{ last_message_preview: null, last_message_direction: null },
} as unknown as Conversation;
const outbound = (id: string, type: string, body: string, changes: Partial<Message> = {}) =>
  ({
    ...{ id, direction: "OUTBOUND", sender_type: "USER", sender_user_id: "u1" },
    ...{ message_type: type, body, client_message_id: `cm-${id}`, delivery_status: "QUEUED" },
    ...{ created_at: "2026-11-04T16:00:00Z", sent_at: null, delivered_at: null },
    ...{ read_at: null, failed_at: null, provider_error_code: null, attachment: null },
    ...changes,
  }) as Message;

type Reply = { status: number; body?: unknown };
function server(media: () => Reply, stored: Message[] = []) {
  const api = mockApi({
    [`GET ${BASE}/`]: { status: 200, body: { results: [rosa], next: null } },
    [`GET ${BASE}/1/`]: { status: 200, body: rosa },
    [`GET ${BASE}/1/messages/`]: () => ({
      status: 200,
      body: { results: [...stored].reverse(), next: null },
    }),
    [MEDIA]: () => {
      const reply = media();
      if (reply.status < 300) stored.push(reply.body as Message);
      return reply;
    },
  });
  // Lo que salió en cada subida: el formulario tal cual lo arma el cliente generado.
  const forms = () =>
    api.mock.calls
      .filter(([url, init]) => String(url).endsWith("/1/media/") && init?.method === "POST")
      .map(([, init]) => init!.body as FormData);
  return { api, forms };
}
const show = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <Inbox />
    </TenantProvider>,
  );
const openChat = async () => {
  fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
  await screen.findByRole("heading", { level: 2 });
};
const pdf = (name = "cotizacion.pdf", size = 1_258_291) => {
  const file = new File(["%PDF-1.7"], name, { type: "application/pdf" });
  Object.defineProperty(file, "size", { value: size });
  return file;
};
const attach = async (file: File) => {
  await screen.findByRole("button", { name: "Adjuntar imagen o PDF" });
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(input, { target: { files: [file] } });
};
const caption = () => screen.findByRole("textbox", { name: "Pie de foto (opcional)" });
const send = (name = "Enviar") => fireEvent.click(screen.getByRole("button", { name }));

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("adjuntar desde el cuadro de redacción (F7-09b3)", () => {
  it("un PDF sin pie de foto sale sin el campo `caption` y queda como documento en cola", async () => {
    const s = server(() => ({ status: 202, body: outbound("s1", "DOCUMENT", "Documento") }));
    show();
    await openChat();
    await attach(pdf());
    // El adjunto se ve con su nombre y tamaño; el texto pasa a ser el pie de foto (opcional).
    const box = await caption();
    expect(screen.getByText("cotizacion.pdf")).toBeVisible();
    expect(box).toHaveAccessibleDescription(/Adjunto: cotizacion\.pdf, 1[.,]2 MB/);
    expect(screen.getByText(/de 1024 caracteres/)).toBeInTheDocument();
    send();
    await waitFor(() => expect(s.forms()).toHaveLength(1));
    const form = s.forms()[0]!;
    expect(form.has("caption")).toBe(false); // la ruta responde 400 a `caption=""`
    expect((form.get("file") as File).name).toBe("cotizacion.pdf");
    expect(form.get("client_message_id")).toMatch(/^[0-9a-f-]{36}$/);
    const log = await screen.findByRole("list", { name: "Mensajes" });
    const bubble = (await within(log).findByText("En cola")).closest("li")!;
    expect(bubble).toHaveTextContent(/^Documento/);
    expect(bubble.textContent?.match(/Documento/g)).toHaveLength(1); // la etiqueta no se repite
    await screen.findByRole("textbox", { name: "Responder al cliente" }); // el adjunto se quitó
    expect(screen.queryByText("cotizacion.pdf")).toBeNull();
  });

  it("un pie de solo espacios no viaja", async () => {
    const s = server(() => ({ status: 202, body: outbound("s1", "DOCUMENT", "Documento") }));
    show();
    await openChat();
    await attach(pdf());
    fireEvent.change(await caption(), { target: { value: "   " } });
    send();
    await waitFor(() => expect(s.forms()).toHaveLength(1));
    expect(s.forms()[0]!.has("caption")).toBe(false);
  });

  it("el pie cuenta cada salto de línea como dos (CRLF) y los emojis como dos unidades", async () => {
    server(() => ({ status: 500 }));
    show();
    await openChat();
    await attach(pdf());
    fireEvent.change(await caption(), { target: { value: "🐶".repeat(512) } }); // 1024 unidades
    expect(screen.getByRole("button", { name: "Enviar" })).toBeEnabled();
    fireEvent.change(await caption(), { target: { value: "🐶".repeat(513) } });
    expect(screen.getByRole("button", { name: "Enviar" })).toBeDisabled();
    fireEvent.change(await caption(), { target: { value: `${"a".repeat(1023)}\n` } }); // 1025 con el CRLF
    expect(screen.getByRole("button", { name: "Enviar" })).toBeDisabled();
  });

  it("los topes exactos se adjuntan (5 MB una imagen, 10 MB un PDF)", async () => {
    server(() => ({ status: 500 }));
    show();
    await openChat();
    await attach(pdf("limite.pdf", 10 << 20));
    expect(await caption()).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Quitar el archivo" }));
    const png = new File(["x"], "limite.png", { type: "image/png" });
    Object.defineProperty(png, "size", { value: 5 << 20 });
    await attach(png);
    expect(await caption()).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("otro archivo con el mismo pie es otro intento: otro id", async () => {
    let reply: Reply = { status: 500, body: { code: "INTERNAL_ERROR" } };
    const s = server(() => reply);
    show();
    await openChat();
    await attach(pdf());
    fireEvent.change(await caption(), { target: { value: "Su cotización" } });
    send();
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Quitar el archivo" }));
    await attach(pdf("otra.pdf"));
    reply = { status: 202, body: outbound("s1", "DOCUMENT", "Su cotización") };
    send(); // quitar el archivo deja de ser un reintento
    await waitFor(() => expect(s.forms()).toHaveLength(2));
    const [first, second] = s.forms();
    expect(second!.get("caption")).toBe("Su cotización");
    expect(second!.get("client_message_id")).not.toBe(first!.get("client_message_id"));
  });

  it("una imagen con pie de foto lo envía tal cual; solo espacios no cuenta como pie", async () => {
    const s = server(() => ({ status: 202, body: outbound("s1", "IMAGE", "Collar rojo") }));
    show();
    await openChat();
    const photo = new File(["\xff\xd8\xff"], "collar.jpg", { type: "image/jpeg" });
    await attach(photo);
    fireEvent.change(await caption(), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Enviar" })).toBeEnabled(); // el archivo basta
    fireEvent.change(await caption(), { target: { value: "Collar rojo" } });
    send();
    await waitFor(() => expect(s.forms()).toHaveLength(1));
    expect(s.forms()[0]!.get("caption")).toBe("Collar rojo");
    const log = await screen.findByRole("list", { name: "Mensajes" });
    const text = await within(log).findByText("Collar rojo");
    expect(text.closest("p")).not.toHaveClass("italic"); // el pie es texto de quien envía
    expect(text.closest("li")).toHaveTextContent(/^Imagen/);
  });

  it.each([
    ["un GIF", new File(["GIF89a"], "perro.gif", { type: "image/gif" }), "Solo se pueden enviar"],
    [
      "una imagen de más de 5 MB",
      (() => {
        const big = new File(["x"], "foto.png", { type: "image/png" });
        Object.defineProperty(big, "size", { value: (5 << 20) + 1 });
        return big;
      })(),
      "El archivo es demasiado grande",
    ],
    ["un PDF de más de 10 MB", pdf("grande.pdf", (10 << 20) + 1), "El archivo es demasiado grande"],
  ])("%s no se adjunta y se explica sin subir nada", async (_name, file, text) => {
    const s = server(() => ({ status: 500 }));
    show();
    await openChat();
    await attach(file);
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByRole("textbox", { name: "Pie de foto (opcional)" })).toBeNull();
    expect(screen.getByRole("button", { name: "Enviar" })).toBeDisabled();
    expect(s.forms()).toHaveLength(0);
  });

  it.each([
    [413, { code: "MEDIA_TOO_LARGE" }, "demasiado grande"],
    [413, undefined, "demasiado grande"], // el tope del proxy responde sin el `code` de la API
    [415, { code: "MEDIA_TYPE_NOT_ALLOWED" }, "Solo se pueden enviar"],
    [415, undefined, "Solo se pueden enviar"],
  ])("un %i quita el archivo y se explica, sin «Reintentar»", async (status, body, text) => {
    const s = server(() => ({ status, body }));
    show();
    await openChat();
    await attach(pdf());
    fireEvent.change(await caption(), { target: { value: "Su cotización" } });
    send();
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByText("cotizacion.pdf")).toBeNull();
    expect(screen.queryByRole("button", { name: "Reintentar" })).toBeNull();
    // El texto queda como respuesta: enviarlo ya no es un medio.
    expect(await screen.findByRole("textbox", { name: "Responder al cliente" })).toHaveValue(
      "Su cotización",
    );
    expect(s.forms()).toHaveLength(1);
  });

  it.each([
    [411, { code: "LENGTH_REQUIRED" }, "no llegó completo"],
    [411, undefined, "no llegó completo"],
    [409, { code: "WINDOW_CLOSED" }, "Ya pasó la ventana de atención"],
    [429, { code: "RATE_LIMITED" }, "Demasiados intentos"],
  ])(
    "un %i se explica por su código y «Reintentar» reutiliza el id",
    async (status, body, text) => {
      let reply: Reply = { status, body };
      const s = server(() => reply);
      show();
      await openChat();
      await attach(pdf());
      send();
      expect(await screen.findByRole("alert")).toHaveTextContent(text);
      expect(screen.queryByText("En cola")).toBeNull(); // el pendiente se quitó
      expect(screen.getByText("cotizacion.pdf")).toBeVisible(); // el adjunto se conserva
      reply = { status: 202, body: outbound("s1", "DOCUMENT", "Documento") };
      send("Reintentar");
      await waitFor(() => expect(s.forms()).toHaveLength(2));
      const [first, second] = s.forms();
      expect(second!.get("client_message_id")).toBe(first!.get("client_message_id"));
    },
  );

  it("con el envío sin antivirus apagado (409 MEDIA_NOT_SCANNED) lo explica y no deja adjuntar", async () => {
    const s = server(() => ({ status: 409, body: { code: "MEDIA_NOT_SCANNED" } }));
    show();
    await openChat();
    await attach(pdf());
    const reads = () =>
      s.api.mock.calls.filter(
        ([url, init]) => String(url).endsWith("/1/messages/") && init?.method === "GET",
      ).length;
    const before = reads();
    send();
    // El envío canceló la lectura en curso: tras el fallo se vuelve a pedir (no queda «Cargando…»).
    await waitFor(() => expect(reads()).toBeGreaterThan(before));
    const [why] = await screen.findAllByText(/Por ahora no se pueden enviar archivos/, {
      selector: "p:not([role])",
    });
    expect(why).toBeVisible();
    // También lo anuncia la región viva que decía «Enviando…» (WCAG 4.1.3).
    const regions = screen.getAllByRole("status").map((region) => region.textContent);
    expect(regions).toContainEqual(expect.stringMatching(/Por ahora no se pueden enviar archivos/));
    expect(screen.queryByRole("alert")).toBeNull(); // un aviso fijo, no un error del envío
    expect(screen.getByRole("button", { name: "Adjuntar imagen o PDF" })).toHaveAttribute(
      "title",
      expect.stringMatching(/Por ahora no se pueden enviar archivos/),
    );
    expect(screen.queryByText("cotizacion.pdf")).toBeNull();
    const clip = screen.getByRole("button", { name: "Adjuntar imagen o PDF" });
    expect(clip).toHaveAttribute("aria-disabled", "true");
    expect(clip).toHaveAccessibleDescription(/Por ahora no se pueden enviar archivos/);
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    const opened = vi.spyOn(input, "click");
    fireEvent.click(clip);
    expect(opened).not.toHaveBeenCalled();
    expect(s.forms()).toHaveLength(1);
  });

  it("la burbuja de un medio saliente muestra su clase y su estado de entrega", async () => {
    server(
      () => ({ status: 500 }),
      [
        outbound("m1", "DOCUMENT", "Documento", { delivery_status: "DELIVERED" }),
        outbound("m2", "IMAGE", "Foto del pedido", { delivery_status: "READ" }),
        outbound("m3", "IMAGE", "Imagen", {
          ...{ delivery_status: "FAILED", provider_error_code: "MEDIA_NOT_SCANNED" },
        }),
      ],
    );
    show();
    await openChat();
    const log = await screen.findByRole("list", { name: "Mensajes" });
    const [document_, image, failed] = await within(log).findAllByRole("listitem");
    expect(document_).toHaveTextContent(/^Documento.*Entregado$/);
    expect(document_).not.toHaveTextContent("todavía no se puede mostrar");
    expect(document_).toHaveClass("bg-accent");
    expect(image).toHaveTextContent(/^ImagenFoto del pedido.*Leído$/);
    expect(failed).toHaveTextContent("No se pudo enviar");
    expect(failed).toHaveTextContent("El archivo no se envió: aún no pasó el antivirus.");
    expect(log.querySelector("img, a, iframe, object, embed")).toBeNull();
  });
});
