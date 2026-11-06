import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { ChannelAccount, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ChannelsList } from "./channels-list";

const BASE = "/api/v1/o/acme/channels";
const LIST = `GET ${BASE}/`;
const CONNECT = `POST ${BASE}/whatsapp/`;
const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
const SECRET = "EAAGsecretTOKEN0123456789";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "channels.manage", scopes: [] }],
};
const account = (id: string, changes: Partial<ChannelAccount> = {}) =>
  ({
    ...{ id, platform: "WHATSAPP", name: "Tienda Lima", status: "PENDING" },
    ...{ status_detail: "WEBHOOK_NOT_SEEN", quality_rating: null, messaging_limit_tier: null },
    ...{ last_healthcheck_at: null, external_account_id: "109876543210", token_last_four: "ABCD" },
    ...{ external_parent_id: "208765432109", version: 1, created_at: "2026-11-01T10:00:00Z" },
    ...{ updated_at: "2026-11-01T10:00:00Z", ...changes },
  }) as ChannelAccount;
const sandbox = account("s1", {
  ...{ platform: "SANDBOX", name: "Sandbox", status: "CONNECTED", status_detail: null },
  ...{ external_parent_id: null, token_last_four: null },
});
const wa = account("a1");
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const show = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <ChannelsList />
    </TenantProvider>,
  );
// Todo lo que viajó por la red: la URL, el método y el cuerpo.
const sent = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.map(([url, init]) => ({
    url: String(url),
    method: init?.method,
    body: init?.body,
  }));
const field = (label: RegExp) => screen.getByLabelText(label) as HTMLInputElement;
const fillConnect = (token = SECRET) => {
  fireEvent.change(field(/^Nombre de la cuenta/), { target: { value: "Tienda Cusco" } });
  fireEvent.change(field(/^ID de la cuenta de WhatsApp Business/), {
    target: { value: "208765432109" },
  });
  fireEvent.change(field(/^ID del número de teléfono/), { target: { value: "109876543211" } });
  fireEvent.change(field(/^Token de acceso/), { target: { value: token } });
};
const open = async () => {
  fireEvent.click(await screen.findByRole("button", { name: "Conectar WhatsApp" }));
  await screen.findByRole("form", { name: "Conectar una cuenta de WhatsApp" });
};
const submit = () => fireEvent.click(screen.getByRole("button", { name: "Conectar" }));
const noSecret = () => {
  expect(new XMLSerializer().serializeToString(document.body)).not.toContain(SECRET); // texto y atributos
  expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET);
  expect(JSON.stringify({ ...sessionStorage })).not.toContain(SECRET);
  expect(location.href).not.toContain(SECRET);
};

beforeAll(() => {
  // jsdom no implementa el `<dialog>` modal (el del código de segundo factor): se refleja `open`.
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
});
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Canales (F7-07b)", () => {
  it("lista las cuentas con su estado, el checklist y solo los últimos cuatro del token", async () => {
    const live = account("a2", {
      ...{ name: "Tienda Arequipa", status: "CONNECTED", status_detail: null },
      ...{ quality_rating: "GREEN", messaging_limit_tier: "TIER_1K" },
      ...{ last_healthcheck_at: "2026-11-05T15:30:00Z", token_last_four: "WXYZ" },
    });
    const broken = account("a3", { name: "Rota", status: "ERROR", status_detail: "TOKEN_INVALID" });
    mockApi({ [LIST]: page([sandbox, wa, live, broken]) });
    show();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando canales");
    // una por cuenta (los pasos del checklist son otras listas)
    const items = [
      ...(await screen.findByRole("list", { name: "Canales" })).querySelectorAll(":scope > li"),
    ] as HTMLElement[];
    expect(items[0]).toHaveTextContent("Canal de pruebas"); // la Sandbox, claramente
    expect(within(items[0]!).queryByRole("button")).toBeNull(); // la Sandbox no tiene acciones
    expect(items[1]).toHaveTextContent("Pendiente");
    expect(items[1]).toHaveTextContent("Todavía no llega ningún mensaje de Meta a esta cuenta.");
    expect(within(items[1]!).getByText("Token válido")).toBeVisible();
    expect(within(items[1]!).getByText("Falta recibir el primer mensaje de Meta")).toBeVisible();
    expect(items[1]).toHaveTextContent("…ABCD"); // solo el final
    expect(items[1]).toHaveTextContent("Todavía no se comprobó");
    expect(items[2]).toHaveTextContent("Conectada");
    expect(items[2]).toHaveTextContent("…WXYZ");
    expect(items[2]).toHaveTextContent("CalidadBuena");
    expect(items[2]).toHaveTextContent("Nivel de mensajería1 000");
    expect(within(items[2]!).getByText("Mensajes de Meta recibidos")).toBeVisible();
    expect(items[3]).toHaveTextContent("Con error");
    expect(within(items[3]!).getByText("Falta un token válido")).toBeVisible();
    expect(items[3]).toHaveTextContent("El token ya no es válido: rótalo");
  });

  it("los nombres de cuenta van como texto aislado, nunca como HTML", async () => {
    const hostile = account("a9", { name: "<img src=x onerror=alert(1)>‮abc" });
    mockApi({ [LIST]: page([hostile]) });
    const { container } = show();
    await screen.findByRole("list", { name: "Canales" });
    const bdi = container.querySelector("li bdi")!;
    expect(bdi.textContent).toBe("<img src=x onerror=alert(1)>‮abc");
    expect(bdi).toHaveAttribute("dir", "auto");
    expect(container.querySelector("img")).toBeNull();
  });

  it("un 403 de la lista se explica y no ofrece conectar", async () => {
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver los canales de esta organización.",
    );
    expect(screen.queryByRole("button", { name: "Conectar WhatsApp" })).toBeNull();
  });

  it("el token es un campo de contraseña sin autocompletado, y se vacía al enviar", async () => {
    const api = mockApi({
      [LIST]: page([sandbox]),
      [CONNECT]: { status: 409, body: { code: "CHANNEL_NOT_ENABLED" } },
    });
    show();
    await open();
    const token = field(/^Token de acceso/);
    expect(token).toHaveAttribute("type", "password");
    expect(token).toHaveAttribute("autocomplete", "new-password");
    expect(token).toHaveAttribute("spellcheck", "false");
    expect(token).not.toHaveAttribute("aria-label");
    expect(token).not.toHaveAttribute("title");
    fillConnect();
    expect(token.value).toBe(SECRET);
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El canal de WhatsApp no está habilitado en este despliegue.",
    );
    expect(field(/^Token de acceso/).value).toBe(""); // también con un error
    expect(field(/^Nombre de la cuenta/).value).toBe("Tienda Cusco"); // lo demás se conserva
    noSecret();
    // el token viajó una vez, en el cuerpo del POST
    const posts = sent(api).filter(
      (call) => call.method === "POST" && call.url.endsWith("/whatsapp/"),
    );
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0]!.body))).toEqual({
      ...{ name: "Tienda Cusco", waba_id: "208765432109", phone_number_id: "109876543211" },
      access_token: SECRET,
    });
    expect(sent(api).filter((call) => call.url.includes(SECRET))).toEqual([]); // nunca en una URL
  });

  it("conectar: el aviso, la lista vuelve a pedirse y el token no queda en ninguna parte", async () => {
    let listed = 0;
    const api = mockApi({
      [LIST]: () => {
        listed += 1;
        return page(listed === 1 ? [sandbox] : [sandbox, wa]);
      },
      [CONNECT]: { status: 201, body: { ...wa, display_identifier: "x", token_last_four: "ABCD" } },
    });
    show();
    await open();
    fillConnect();
    submit();
    expect(
      await screen.findByText("«Tienda Cusco» quedó conectada y pendiente de su primer mensaje."),
    ).toBeVisible();
    expect(await screen.findByText("Tienda Lima")).toBeVisible(); // la lista nueva
    expect(screen.queryByRole("form", { name: "Conectar una cuenta de WhatsApp" })).toBeNull();
    expect(listed).toBe(2);
    noSecret();
    expect(sent(api).filter((call) => String(call.body).includes(SECRET))).toHaveLength(1);
  });

  it.each([
    ["CHANNEL_PROVIDER_UNAVAILABLE", 503, "Meta no responde ahora"],
    ["CHANNEL_CREDENTIALS_REJECTED", 400, "Meta no aceptó ese token"],
  ])("%s se explica por su código", async (code, status, text) => {
    mockApi({
      [LIST]: page([]),
      [CONNECT]: { status, body: { code, detail: "TEXTO DEL SERVIDOR" } },
    });
    show();
    await open();
    fillConnect();
    submit();
    expect(await screen.findByText(new RegExp(text))).toBeVisible();
    expect(document.body.textContent).not.toContain("TEXTO DEL SERVIDOR"); // nunca el texto ajeno
  });

  it("un número no registrable da siempre el mismo mensaje genérico, junto a su campo", async () => {
    mockApi({
      [LIST]: page([]),
      [CONNECT]: {
        status: 400,
        body: { code: "VALIDATION_ERROR", fields: { phone_number_id: ["otro texto"] } },
      },
    });
    show();
    await open();
    fillConnect();
    submit();
    expect(await screen.findByText("No se puede registrar ese número.")).toBeVisible();
    expect(field(/^ID del número de teléfono/)).toHaveAccessibleDescription(
      "No se puede registrar ese número.",
    );
    expect(document.body.textContent).not.toContain("otro texto");
  });

  it("429 dice cuánto esperar", async () => {
    mockApi({
      [LIST]: page([]),
      [CONNECT]: { status: 429, body: { code: "RATE_LIMITED" }, headers: { "Retry-After": "540" } },
    });
    show();
    await open();
    fillConnect();
    submit();
    expect(
      await screen.findByText("Demasiados intentos. Vuelve a probar en 9 minutos."),
    ).toBeVisible();
  });

  it("STEP_UP_REQUIRED abre el código y repite la petición una vez con el mismo token", async () => {
    let posts = 0;
    const api = mockApi({
      [LIST]: page([]),
      [CONNECT]: () => {
        posts += 1;
        return posts === 1
          ? { status: 403, body: { code: "STEP_UP_REQUIRED" } }
          : { status: 201, body: { ...wa } };
      },
      [STEP_UP]: { status: 200, body: { step_up_expires_in: 900 } },
    });
    show();
    await open();
    fillConnect();
    submit();
    fireEvent.change(await screen.findByLabelText(/^Código/), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verificar" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const bodies = sent(api)
      .filter((call) => call.url === `${BASE}/whatsapp/`)
      .map((call) => JSON.parse(String(call.body)).access_token);
    expect(bodies).toEqual([SECRET, SECRET]); // el mismo, una sola repetición
    expect(await screen.findByText(/quedó conectada/)).toBeVisible();
    noSecret();
  });

  it("cancelar el código no es un fallo: el formulario sigue y el token ya se borró", async () => {
    mockApi({ [LIST]: page([]), [CONNECT]: { status: 403, body: { code: "STEP_UP_REQUIRED" } } });
    show();
    await open();
    fillConnect();
    submit();
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(field(/^Token de acceso/).value).toBe("");
  });
});
