import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { ChannelAccount, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ChannelsList } from "./channels-list";

const BASE = "/api/v1/o/acme/channels";
const LIST = `GET ${BASE}/`;
const CONNECT = `POST ${BASE}/whatsapp/`;
const ROTATE = `POST ${BASE}/whatsapp/a1/token/`;
const DISCONNECT = `DELETE ${BASE}/whatsapp/a1/`;
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
// Cuántas veces se pidió la lista (la primera carga y cada refresco).
const listReads = (api: ReturnType<typeof mockApi>) =>
  sent(api).filter((call) => call.method === "GET" && call.url === `${BASE}/`).length;
// Los formularios y confirmaciones aparecen tras una acción: se espera a que estén antes de usarlos.
const openRotate = async () => {
  fireEvent.click(await screen.findByRole("button", { name: "Rotar token" }));
  return screen.findByRole("form", { name: "Rotar el token" });
};
const rotate = (token = SECRET) => {
  fireEvent.change(field(/^Token nuevo/), { target: { value: token } });
  fireEvent.click(screen.getByRole("button", { name: "Rotar" }));
};
const askDisconnect = async () => {
  fireEvent.click(await screen.findByRole("button", { name: "Desconectar" }));
  return screen.findByRole("group", { name: "Desconectar Tienda Lima" });
};
const submit = () => fireEvent.click(screen.getByRole("button", { name: "Conectar" }));
const noSecret = () => {
  expect(new XMLSerializer().serializeToString(document.body)).not.toContain(SECRET); // texto y atributos
  expect(JSON.stringify({ ...localStorage })).not.toContain(SECRET);
  expect(JSON.stringify({ ...sessionStorage })).not.toContain(SECRET);
  expect(location.href).not.toContain(SECRET);
};

// Estos tests esperan peticiones (la API simulada, el CSRF y el código de segundo factor) y el
// montaje de un `<dialog>`: en una máquina lenta (CI) el segundo por omisión no alcanza. Se sube el
// tiempo que esperan los `findBy…` y `waitFor` de este archivo, sin reintentar nada: la espera
// termina en cuanto aparece lo esperado.
configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

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
    expect(within(items[0]!).queryByRole("button")).toBeNull(); // sin rotar ni desconectar
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

  it("un código de error desconocido da el mensaje genérico, nunca el código ni el texto del servidor", async () => {
    mockApi({
      [LIST]: page([]),
      [CONNECT]: {
        status: 500,
        body: { code: "CODIGO_NUEVO_DEL_SERVIDOR", detail: "TEXTO AJENO" },
      },
    });
    show();
    await open();
    fillConnect();
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(document.body.textContent).not.toContain("CODIGO_NUEVO_DEL_SERVIDOR");
    expect(document.body.textContent).not.toContain("TEXTO AJENO");
  });

  it("el formulario con el token se envía por POST aunque falle el JavaScript", async () => {
    mockApi({ [LIST]: page([]) });
    show();
    await open();
    expect(screen.getByRole("form", { name: "Conectar una cuenta de WhatsApp" })).toHaveAttribute(
      "method",
      "post",
    );
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
    // el `<dialog>` solo es accesible cuando está abierto: se espera a eso y no a una de sus etiquetas
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Código/), { target: { value: "123456" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Verificar" }));
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

  it("rotar el token: campo de contraseña por POST, un solo envío, el campo vacío y el foco de vuelta", async () => {
    const logged = (["log", "info", "warn", "error", "debug"] as const).map((level) =>
      vi.spyOn(console, level).mockImplementation(() => {}),
    );
    const api = mockApi({ [LIST]: page([wa]), [ROTATE]: { status: 200, body: { ...wa } } });
    show();
    const form = await openRotate();
    expect(form).toHaveAttribute("method", "post"); // un envío nativo nunca llevaría el token a la URL
    const token = field(/^Token nuevo/);
    expect(token).toHaveAttribute("type", "password");
    expect(token).toHaveAttribute("autocomplete", "new-password");
    expect(token).not.toHaveAttribute("aria-label");
    expect(token).not.toHaveAttribute("title");
    rotate();
    expect(await screen.findByText("El token se rotó.")).toBeVisible();
    await waitFor(() => expect(listReads(api)).toBe(2)); // la lista se vuelve a pedir tras rotar
    const posts = sent(api).filter((call) => call.method === "POST");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(String(posts[0]!.body))).toEqual({ access_token: SECRET });
    noSecret();
    await waitFor(() => expect(screen.getByRole("button", { name: "Rotar token" })).toHaveFocus());
    // nunca en la consola
    expect(JSON.stringify(logged.flatMap((spy) => spy.mock.calls))).not.toContain(SECRET);
    vi.restoreAllMocks();
  });

  it.each([
    [409, "CHANNEL_NOT_ENABLED", /no está habilitado en este despliegue/],
    [503, "CHANNEL_PROVIDER_UNAVAILABLE", /Meta no responde ahora/],
    [400, "CHANNEL_CREDENTIALS_REJECTED", /Meta no aceptó ese token/],
    [403, "PERMISSION_DENIED", /No tienes permiso para gestionar los canales/],
    [500, "CODIGO_NUEVO_DEL_SERVIDOR", /Algo salió mal de nuestro lado/],
  ])(
    "rotar: un %i %s se explica por su código y el token escrito ya se borró",
    async (status, code, text) => {
      mockApi({ [LIST]: page([wa]), [ROTATE]: { status, body: { code, detail: "TEXTO AJENO" } } });
      show();
      await openRotate();
      rotate();
      expect(await screen.findByRole("alert")).toHaveTextContent(text);
      expect(field(/^Token nuevo/).value).toBe("");
      expect(document.body.textContent).not.toContain("TEXTO AJENO");
      expect(document.body.textContent).not.toContain("CODIGO_NUEVO");
      noSecret();
    },
  );

  it("rotar: 429 dice cuánto esperar", async () => {
    mockApi({
      [LIST]: page([wa]),
      [ROTATE]: { status: 429, body: { code: "RATE_LIMITED" }, headers: { "Retry-After": "300" } },
    });
    show();
    await openRotate();
    rotate();
    expect(
      await screen.findByText("Demasiados intentos. Vuelve a probar en 5 minutos."),
    ).toBeVisible();
  });

  it("rotar: STEP_UP_REQUIRED pide el código y repite la petición una sola vez con el mismo token", async () => {
    let posts = 0;
    const api = mockApi({
      [LIST]: page([wa]),
      [ROTATE]: () => {
        posts += 1;
        return posts === 1
          ? { status: 403, body: { code: "STEP_UP_REQUIRED" } }
          : { status: 200, body: { ...wa } };
      },
      [STEP_UP]: { status: 200, body: { step_up_expires_in: 900 } },
    });
    show();
    await openRotate();
    rotate();
    // el `<dialog>` solo es accesible cuando está abierto: se espera a eso y se consulta dentro
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Código/), { target: { value: "123456" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Verificar" }));
    expect(await screen.findByText("El token se rotó.")).toBeVisible();
    const bodies = sent(api)
      .filter((call) => call.url === `${BASE}/whatsapp/a1/token/`)
      .map((call) => JSON.parse(String(call.body)).access_token);
    expect(bodies).toEqual([SECRET, SECRET]); // el mismo, una sola repetición
    noSecret();
  });

  it("rotar: cancelar el código no es un fallo y el token ya se borró", async () => {
    mockApi({ [LIST]: page([wa]), [ROTATE]: { status: 403, body: { code: "STEP_UP_REQUIRED" } } });
    show();
    await openRotate();
    rotate();
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
    expect(field(/^Token nuevo/).value).toBe("");
  });

  it("desconectar pide confirmación que explica el efecto, con el foco en «Cancelar»", async () => {
    let listed = 0;
    const api = mockApi({
      [LIST]: () => {
        listed += 1;
        return page(listed === 1 ? [wa] : []);
      },
      [DISCONNECT]: { status: 204 },
    });
    show();
    const group = await askDisconnect();
    expect(group).toHaveTextContent("¿Desconectar «Tienda Lima»?");
    expect(group).toHaveTextContent("Reconectarla es crear una cuenta nueva.");
    await waitFor(() =>
      expect(within(group).getByRole("button", { name: "Cancelar" })).toHaveFocus(),
    );
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" })); // no hace nada
    expect(sent(api).filter((call) => call.method === "DELETE")).toEqual([]);
    await waitFor(() => expect(screen.getByRole("button", { name: "Desconectar" })).toHaveFocus());
    const again = await askDisconnect();
    fireEvent.click(within(again).getByRole("button", { name: "Sí, desconectar" }));
    await waitFor(() => expect(screen.queryByText("Tienda Lima")).toBeNull()); // la lista nueva
    expect(sent(api).filter((call) => call.method === "DELETE")).toHaveLength(1);
  });

  it.each([
    [404, "NOT_FOUND", "Esa cuenta ya no existe. Actualizamos la lista."],
    [403, "PERMISSION_DENIED", "No tienes permiso para gestionar los canales."],
    [
      500,
      "CODIGO_NUEVO_DEL_SERVIDOR",
      "Algo salió mal de nuestro lado. Inténtalo de nuevo en unos minutos.",
    ],
  ])("desconectar: un %i %s se explica por su código", async (status, code, text) => {
    const api = mockApi({
      [LIST]: page([wa]),
      [DISCONNECT]: { status, body: { code, detail: "TEXTO AJENO" } },
    });
    show();
    const group = await askDisconnect();
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desconectar" }));
    expect(await screen.findByText(text)).toBeVisible();
    // solo un 404 (la cuenta ya no está) vuelve a leer la lista
    if (status === 404) await waitFor(() => expect(listReads(api)).toBe(2));
    else expect(listReads(api)).toBe(1);
    expect(document.body.textContent).not.toContain("TEXTO AJENO");
    expect(document.body.textContent).not.toContain("CODIGO_NUEVO");
  });

  it("la Sandbox y una lista sin permiso no ofrecen acciones", async () => {
    mockApi({ [LIST]: page([sandbox]) });
    show();
    const card = (await screen.findByText("Canal de pruebas", { exact: false })).closest("li")!;
    expect(within(card).queryByRole("button")).toBeNull();
    cleanup();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver los canales",
    );
    expect(screen.queryByRole("button", { name: "Rotar token" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Desconectar" })).toBeNull();
  });
});
