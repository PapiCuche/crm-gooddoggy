import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { MessageAttachment, SelfContext } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { MediaFile } from "./media-file";

const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "conversations.view", scopes: [] }],
};
const ROUTE = "/api/v1/o/acme/conversations/c9/attachments/a1/url/";
const SIGNED = "http://localhost:3900/crm-local/org/o1/message-media/x?X-Amz-Signature=abc";
const file = (changes: Partial<MessageAttachment> = {}) =>
  ({
    ...{ id: "a1", kind: "IMAGE", status: "DONE", mime_type: "image/jpeg" },
    ...{ size_bytes: 1_258_291, available: true, ...changes },
  }) as MessageAttachment;

let replies: Array<() => Promise<Response>>;
let fetchMock: ReturnType<typeof vi.fn>;
let opened: HTMLAnchorElement[];
const reply = (status: number, body: unknown) => async () =>
  new Response(JSON.stringify(body), { status });

beforeEach(() => {
  replies = [];
  fetchMock = vi.fn(async () => (replies.shift() ?? reply(500, { code: "X" }))());
  vi.stubGlobal("fetch", fetchMock);
  opened = [];
  // jsdom no navega: se captura el enlace temporal en vez de seguirlo.
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    opened.push(this);
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const show = (attachment: MessageAttachment) =>
  renderApp(
    <TenantProvider value={tenant}>
      <ul>
        <MediaFile conversationId="c9" attachment={attachment} />
      </ul>
    </TenantProvider>,
  );
const none = "img, audio, video, a, iframe, object, embed, source, track";

describe("MediaFile (F7-09a3)", () => {
  it.each([
    ["PENDING", "Recibiendo el archivo del canal…"],
    ["DOWNLOADING", "Recibiendo el archivo del canal…"],
    ["DONE", "Todavía no se puede abrir: primero tiene que pasar la revisión de seguridad."],
    ["FAILED", "No se pudo recibir el archivo. Pide al cliente que lo vuelva a enviar."],
    ["EXPIRED", "caducó en el canal antes de recibirlo."],
    ["constructor", "Archivo no disponible."],
    ["RARO", "Archivo no disponible."],
  ])("%s sin `available` se explica y nunca pide la URL", (status, text) => {
    // `available` es lo único que habilita la descarga, también si el estado dijera otra cosa
    const { container } = show(file({ status: status as never, available: false }));
    expect(container).toHaveTextContent(text);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("button")).toBeNull();
    expect(container.querySelector(none)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("disponible: un GET al pulsar, un enlace temporal sin Referer y la URL en ningún sitio", async () => {
    replies.push(reply(200, { url: SIGNED, expires_at: "2026-11-13T10:00:00Z" }));
    const { container, client } = show(file());
    const button = screen.getByRole("button", { name: "Descargar el archivo (JPEG · 1.2 MB)" });
    expect(container).toHaveTextContent("JPEG · 1.2 MB");
    expect(fetchMock).not.toHaveBeenCalled(); // nada antes de pulsar
    fireEvent.click(button);
    await waitFor(() => expect(opened).toHaveLength(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [[url, init]] = fetchMock.mock.calls as [[string, RequestInit]];
    expect(String(url)).toBe(ROUTE);
    expect(init).toMatchObject({ method: "GET" });
    const link = opened[0]!;
    expect(link.href).toBe(SIGNED);
    expect(link.rel).toBe("noopener noreferrer");
    expect(link.referrerPolicy).toBe("no-referrer");
    expect(link.isConnected).toBe(false);
    expect(container.querySelector(none)).toBeNull();
    const attributes = [...container.querySelectorAll("*")].flatMap((el) => [...el.attributes]);
    expect(attributes.map((attribute) => attribute.value).join(" ")).not.toContain("X-Amz");
    expect(container.textContent).not.toContain("X-Amz");
    await waitFor(() => expect(button).toHaveTextContent("Descargar"));
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    for (const mutation of client.getMutationCache().getAll()) {
      expect(mutation.state.data).toBeUndefined();
    }
    expect(
      JSON.stringify(
        client
          .getMutationCache()
          .getAll()
          .map((m) => m.state),
      ),
    ).not.toContain("X-Amz");
  });

  it("dos pulsaciones mientras se pide son una sola petición", async () => {
    let release: (value: Response) => void = () => {};
    replies.push(() => new Promise((resolve) => (release = resolve)));
    show(file());
    const button = screen.getByRole("button", { name: /Descargar el archivo/ });
    fireEvent.click(button);
    await waitFor(() => expect(button).toHaveTextContent("Abriendo…"));
    expect(button).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(button);
    fireEvent.click(button);
    release(new Response(JSON.stringify({ url: SIGNED, expires_at: "x" }), { status: 200 }));
    await waitFor(() => expect(opened).toHaveLength(1));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("un 404 dice solo que no se puede abrir ahora; volver a pulsar vuelve a pedir", async () => {
    replies.push(reply(404, { code: "NOT_FOUND", detail: "a1" }));
    replies.push(reply(200, { url: SIGNED, expires_at: "x" }));
    show(file());
    fireEvent.click(screen.getByRole("button", { name: /Descargar el archivo/ }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      /^No se pudo abrir el archivo ahora\. Vuelve a intentarlo en unos segundos\.$/,
    );
    expect(opened).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /Descargar el archivo/ }));
    await waitFor(() => expect(opened).toHaveLength(1));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("una URL que no es http(s) no se abre", async () => {
    replies.push(reply(200, { url: "javascript:alert(1)", expires_at: "x" }));
    show(file());
    fireEvent.click(screen.getByRole("button", { name: /Descargar el archivo/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo abrir el archivo");
    expect(opened).toHaveLength(0);
  });

  it("un 401 lo resuelve la sesión: sin aviso aquí", async () => {
    replies.push(reply(401, { code: "NOT_AUTHENTICATED" }));
    show(file());
    const button = screen.getByRole("button", { name: /Descargar el archivo/ });
    fireEvent.click(button);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(button).toHaveTextContent("Descargar"));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(opened).toHaveLength(0);
  });

  it("sin tipo ni tamaño: solo «Descargar»; un archivo pequeño en KB", () => {
    const { container } = show(file({ mime_type: null, size_bytes: null }));
    expect(screen.getByRole("button", { name: "Descargar" })).toBeVisible();
    expect(container.querySelector("span")).toBeNull();
    cleanup();
    show(file({ mime_type: "audio/ogg", size_bytes: 20_480 }));
    expect(
      screen.getByRole("button", { name: "Descargar el archivo (OGG · 20 kB)" }),
    ).toBeVisible();
  });
});
