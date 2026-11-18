import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { ChannelAccount, SelfContext } from "@/lib/api/model";
import { ApiError } from "@/lib/http";
import { mockApi, renderApp } from "@/test-utils";

import { CHANNEL_CODES, channelError } from "./channel-errors";
import { ChannelsList } from "./channels-list";

// Messenger e Instagram en la pantalla de canales (F7b-06): las rutas de #918 y el `standby` de #919.
const BASE = "/api/v1/o/acme/channels";
const LIST = `GET ${BASE}/`;
const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
const SECRET = "EAAGsocialSECRET0123456789";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: [{ code: "channels.manage", scopes: [] }],
};
const social = (id: string, changes: Partial<ChannelAccount> = {}) =>
  ({
    ...{ id, platform: "INSTAGRAM", name: "IG Tienda", status: "PENDING" },
    ...{ status_detail: "WEBHOOK_NOT_SEEN", quality_rating: null, messaging_limit_tier: null },
    ...{ last_healthcheck_at: null, external_account_id: "100000000000102" },
    ...{ external_parent_id: "100000000000101", token_last_four: "WXYZ" },
    ...{ has_standby_conversations: false, version: 1, created_at: "2026-11-01T10:00:00Z" },
    ...{ updated_at: "2026-11-01T10:00:00Z", ...changes },
  }) as ChannelAccount;
const ig = social("i1");
const fb = social("m1", {
  ...{ platform: "MESSENGER", name: "Página Tienda", external_account_id: "100000000000101" },
  external_parent_id: null,
});
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const show = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <ChannelsList />
    </TenantProvider>,
  );
const sent = (api: ReturnType<typeof mockApi>) =>
  api.mock.calls.map(([url, init]) => ({
    url: String(url),
    method: init?.method,
    body: init?.body,
  }));
const field = (label: RegExp) => screen.getByLabelText(label) as HTMLInputElement;
const type = (label: RegExp, value: string) =>
  fireEvent.change(field(label), { target: { value } });
const open = async (name: string, title: string) => {
  fireEvent.click(await screen.findByRole("button", { name }));
  return screen.findByRole("form", { name: title });
};
const openMessenger = () => open("Conectar Messenger", "Conectar una página de Messenger");
const openInstagram = () => open("Conectar Instagram", "Conectar una cuenta de Instagram");
const fillMessenger = (token = SECRET) => {
  type(/^Nombre de la cuenta/, "Página Lima");
  type(/^ID de la página de Facebook/, "100000000000101");
  type(/^Token de acceso/, token);
};
const fillInstagram = (token = SECRET) => {
  type(/^Nombre de la cuenta/, "IG Lima");
  type(/^ID de la página de Facebook vinculada/, "100000000000101");
  type(/^ID de la cuenta profesional de Instagram/, "100000000000102");
  type(/^Token de acceso/, token);
};
const submit = () => fireEvent.click(screen.getByRole("button", { name: "Conectar" }));
const noSecret = () => {
  expect(new XMLSerializer().serializeToString(document.body)).not.toContain(SECRET);
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain(SECRET);
  expect(location.href).not.toContain(SECRET);
};
const cards = async () =>
  [
    ...(await screen.findByRole("list", { name: "Canales" })).querySelectorAll(":scope > li"),
  ] as HTMLElement[];

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
beforeAll(() => {
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

describe("Messenger e Instagram en Canales (F7b-06)", () => {
  it("sin `channels.manage` (403 de la lista) no se ofrece conectar ninguna plataforma", async () => {
    const api = mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("No tienes permiso");
    for (const name of ["Conectar WhatsApp", "Conectar Messenger", "Conectar Instagram"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
    expect(sent(api).filter((call) => call.method !== "GET")).toEqual([]);
  });

  it("un 403 al conectar se explica y no se reintenta", async () => {
    const api = mockApi({
      [LIST]: page([]),
      [`POST ${BASE}/messenger/`]: { status: 403, body: { code: "PERMISSION_DENIED" } },
    });
    show();
    await openMessenger();
    fillMessenger();
    submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para gestionar los canales.",
    );
    expect(sent(api).filter((call) => call.method === "POST")).toHaveLength(1);
    noSecret();
  });

  it.each([
    ["messenger", openMessenger, fillMessenger, "page_id", "No se puede registrar esa página."],
    [
      "instagram",
      openInstagram,
      fillInstagram,
      "instagram_account_id",
      "No se puede registrar esa cuenta de Instagram.",
    ],
  ] as const)(
    "%s: una cuenta no registrable da un solo texto genérico junto a su campo, nunca el del servidor",
    async (route, openForm, fill, name, text) => {
      mockApi({
        [LIST]: page([]),
        [`POST ${BASE}/${route}/`]: {
          status: 400,
          body: { code: "VALIDATION_ERROR", fields: { [name]: ["ya está en otra organización"] } },
        },
      });
      show();
      await openForm();
      fill();
      submit();
      expect(await screen.findByText(text)).toBeVisible();
      expect(document.querySelector(`input[name=${name}]`)).toHaveAccessibleDescription(text);
      expect(document.body.textContent).not.toContain("otra organización");
      expect(field(/^Token de acceso/).value).toBe("");
      noSecret();
    },
  );

  it.each([
    [
      400,
      "CHANNEL_CREDENTIALS_REJECTED",
      "Meta no aceptó ese token. Revisa que sea el del usuario del sistema y que gestione esa página.",
    ],
    [
      409,
      "CHANNEL_NOT_ENABLED",
      "Los canales de Messenger e Instagram no están habilitados en este despliegue.",
    ],
    [503, "CHANNEL_PROVIDER_UNAVAILABLE", "Meta no responde ahora"],
    [400, "CHANNEL_PAGE_TAKEN_NUEVO", "Algo salió mal de nuestro lado"], // un código que aún no conoce
  ])("conectar Instagram: un %i %s se explica por su código", async (status, code, text) => {
    mockApi({
      [LIST]: page([]),
      [`POST ${BASE}/instagram/`]: { status, body: { code, detail: "TEXTO AJENO" } },
    });
    show();
    await openInstagram();
    fillInstagram();
    submit();
    expect(await screen.findByText(new RegExp(text))).toBeVisible();
    expect(document.body.textContent).not.toMatch(/TEXTO AJENO|CHANNEL_PAGE_TAKEN_NUEVO/);
    noSecret();
  });

  it("conectar Messenger: un POST a su ruta, sin cuenta de Instagram, y la lista nueva", async () => {
    let listed = 0;
    const api = mockApi({
      [LIST]: () => page(++listed === 1 ? [] : [fb]),
      [`POST ${BASE}/messenger/`]: { status: 201, body: { id: "m1" } },
    });
    show();
    const form = await openMessenger();
    expect(form).toHaveAttribute("method", "post");
    expect(within(form).queryByLabelText(/Instagram/)).toBeNull();
    const token = field(/^Token de acceso/);
    expect(token).toHaveAttribute("type", "password");
    expect(token).toHaveAttribute("autocomplete", "new-password");
    fillMessenger();
    submit();
    expect(
      await screen.findByText("«Página Lima» quedó conectada y pendiente de su primer mensaje."),
    ).toBeVisible();
    expect(await screen.findByText("Página Tienda")).toBeVisible();
    const posts = sent(api).filter((call) => call.method === "POST");
    expect(posts.map((call) => call.url)).toEqual([`${BASE}/messenger/`]);
    expect(JSON.parse(String(posts[0]!.body))).toEqual({
      ...{ name: "Página Lima", page_id: "100000000000101", access_token: SECRET },
    });
    expect(sent(api).filter((call) => call.url.includes(SECRET))).toEqual([]);
    noSecret();
  });

  it("conectar Instagram: la página vinculada y la cuenta, por su ruta", async () => {
    const api = mockApi({
      [LIST]: page([]),
      [`POST ${BASE}/instagram/`]: { status: 201, body: { id: "i1" } },
    });
    show();
    await openInstagram();
    fillInstagram();
    submit();
    expect(await screen.findByText(/«IG Lima» quedó conectada/)).toBeVisible();
    const posts = sent(api).filter((call) => call.method === "POST");
    expect(posts.map((call) => call.url)).toEqual([`${BASE}/instagram/`]);
    expect(JSON.parse(String(posts[0]!.body))).toEqual({
      ...{ name: "IG Lima", page_id: "100000000000101" },
      ...{ instagram_account_id: "100000000000102", access_token: SECRET },
    });
    noSecret();
  });

  it("STEP_UP_REQUIRED repite UNA vez con el mismo token; al cancelar, el siguiente envío pide escribirlo otra vez", async () => {
    let posts = 0;
    const api = mockApi({
      [LIST]: page([]),
      [`POST ${BASE}/messenger/`]: () =>
        ++posts === 3
          ? { status: 201, body: { id: "m1" } }
          : { status: 403, body: { code: "STEP_UP_REQUIRED" } },
      [STEP_UP]: { status: 200, body: { step_up_expires_in: 900 } },
    });
    show();
    await openMessenger();
    fillMessenger();
    submit();
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancelar" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull(); // cancelar no es un fallo
    submit(); // nada guardó el token para reintentar: falta y no se envía
    expect(await screen.findByText("Pega el token de acceso.")).toBeVisible();
    expect(posts).toBe(1);
    type(/^Token de acceso/, SECRET);
    submit();
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Código/), { target: { value: "123456" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Verificar" }));
    expect(await screen.findByText(/quedó conectada/)).toBeVisible();
    const tokens = sent(api)
      .filter((call) => call.method === "POST" && call.url === `${BASE}/messenger/`)
      .map((call) => JSON.parse(String(call.body)).access_token);
    expect(tokens).toEqual([SECRET, SECRET, SECRET]); // el primero, y el segundo con su única repetición
    noSecret();
  });

  it("la tarjeta: ids, solo el final del token, el checklist y ningún aviso de standby", async () => {
    mockApi({ [LIST]: page([ig, fb]) });
    show();
    const [first, second] = await cards();
    expect(first).toHaveTextContent("Instagram");
    expect(first).toHaveTextContent("Página de Facebook (ID)100000000000101");
    expect(first).toHaveTextContent("Cuenta de Instagram (ID)100000000000102");
    expect(first).toHaveTextContent("…WXYZ");
    expect(
      within(first!).getByText("Falta confirmar el acceso a los mensajes de Instagram"),
    ).toBeVisible();
    expect(second).toHaveTextContent("Messenger");
    expect(second).toHaveTextContent("Página de Facebook (ID)100000000000101");
    expect(second).not.toHaveTextContent("Cuenta de Instagram");
    expect(second).not.toHaveTextContent("acceso a los mensajes");
    expect(screen.queryByRole("note")).toBeNull();
    expect(within(first!).getByRole("button", { name: "Rotar token" })).toBeVisible();
    expect(within(second!).getByRole("button", { name: "Desconectar" })).toBeVisible();
  });

  it("con un hilo en standby, la cuenta lo avisa: otra app lo controla y nada responde solo", async () => {
    const held = social("i2", {
      ...{ status: "CONNECTED", status_detail: null, has_standby_conversations: true },
    });
    const off = social("i3", { status: "DEGRADED", status_detail: "MESSAGES_ACCESS_OFF" });
    mockApi({ [LIST]: page([held, off]) });
    show();
    const [first, second] = await cards();
    const note = within(first!).getByRole("note");
    expect(note).toHaveTextContent("Otra app controla una conversación");
    expect(note).toHaveTextContent("el CRM no responde nada automáticamente");
    expect(within(first!).getByText("Otra app controla alguna conversación")).toBeVisible();
    expect(within(second!).queryByRole("note")).toBeNull();
    expect(
      within(second!).getByText("El acceso a los mensajes de Instagram está apagado"),
    ).toBeVisible();
    expect(within(second!).getByText("El CRM atiende las conversaciones")).toBeVisible();
  });

  it.each([
    ["instagram", ig],
    ["messenger", fb],
  ] as const)(
    "rotar en %s: su ruta, el aviso de que el otro token no cambia y el token no queda",
    async (route, account) => {
      const api = mockApi({
        [LIST]: page([account]),
        [`POST ${BASE}/${route}/${account.id}/token/`]: { status: 200, body: { id: account.id } },
      });
      show();
      fireEvent.click(await screen.findByRole("button", { name: "Rotar token" }));
      await screen.findByRole("form", { name: "Rotar el token" });
      expect(field(/^Token nuevo/)).toHaveAccessibleDescription(/rotar uno no rota el otro/);
      type(/^Token nuevo/, SECRET);
      fireEvent.click(screen.getByRole("button", { name: "Rotar" }));
      expect(await screen.findByText("El token se rotó.")).toBeVisible();
      const posts = sent(api).filter((call) => call.method === "POST");
      expect(posts.map((call) => call.url)).toEqual([`${BASE}/${route}/${account.id}/token/`]);
      expect(JSON.parse(String(posts[0]!.body))).toEqual({ access_token: SECRET });
      noSecret();
    },
  );

  it("desconectar Messenger: un clic solo pregunta; al confirmar, su ruta y el aviso", async () => {
    let listed = 0;
    const api = mockApi({
      [LIST]: () => page(++listed === 1 ? [fb] : []),
      [`DELETE ${BASE}/messenger/m1/`]: { status: 204 },
    });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Desconectar" }));
    const group = await screen.findByRole("group", { name: "Desconectar Página Tienda" });
    expect(sent(api).filter((call) => call.method === "DELETE")).toEqual([]);
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desconectar" }));
    expect(await screen.findByText(/quedó desconectada/)).toHaveTextContent(
      "«Página Tienda» quedó desconectada: ya no recibe ni envía mensajes.",
    );
    await waitFor(() => expect(screen.queryByRole("button", { name: "Rotar token" })).toBeNull());
    expect(
      sent(api)
        .filter((call) => call.method === "DELETE")
        .map((call) => call.url),
    ).toEqual([`${BASE}/messenger/m1/`]);
  });

  // F7b-06f: los mutantes que sobrevivieron a la revisión de #938 (M04, M15, M19).
  it("desconectar Instagram: un solo DELETE a su ruta aunque se confirme dos veces", async () => {
    let listed = 0;
    const api = mockApi({
      [LIST]: () => page(++listed === 1 ? [ig] : []),
      [`DELETE ${BASE}/instagram/i1/`]: { status: 204 },
    });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Desconectar" }));
    const group = await screen.findByRole("group", { name: "Desconectar IG Tienda" });
    const yes = within(group).getByRole("button", { name: "Sí, desconectar" });
    fireEvent.click(yes);
    fireEvent.click(yes); // el segundo clic llega mientras el primero sigue en vuelo
    expect(await screen.findByText(/quedó desconectada/)).toBeVisible();
    expect(
      sent(api)
        .filter((call) => call.method === "DELETE")
        .map((call) => call.url),
    ).toEqual([`${BASE}/instagram/i1/`]);
  });

  it("el aviso de standby sale solo de `has_standby_conversations`, no del estado", async () => {
    const calm = social("i5", { status: "CONNECTED", status_detail: null });
    const held = social("i6", { has_standby_conversations: true });
    mockApi({ [LIST]: page([calm, held]) });
    show();
    const [first, second] = await cards();
    expect(within(first!).queryByRole("note")).toBeNull();
    expect(within(first!).getByText("El CRM atiende las conversaciones")).toBeVisible();
    expect(within(second!).getByRole("note")).toHaveTextContent("Otra app controla");
  });

  it("el aviso de desconectada aísla el nombre: un carácter de dirección no lo reordena", async () => {
    let listed = 0;
    const named = social("m1", { ...fb, name: "Tienda\u202Eabc" });
    mockApi({
      [LIST]: () => page(++listed === 1 ? [named] : []),
      [`DELETE ${BASE}/messenger/m1/`]: { status: 204 },
    });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Desconectar" }));
    const group = await screen.findByRole("group", { name: /^Desconectar / });
    fireEvent.click(within(group).getByRole("button", { name: "Sí, desconectar" }));
    const done = await screen.findByText(/quedó desconectada/);
    const bdi = done.querySelector("bdi")!;
    expect(bdi.textContent).toBe("Tienda\u202Eabc");
    expect(bdi).toHaveAttribute("dir", "auto");
  });

  it.each([
    ["instagram", ig],
    ["messenger", fb],
  ] as const)(
    "%s: el 404 al rotar o desconectar se sigue leyendo cuando la tarjeta ya no está",
    async (route, account) => {
      let listed = 0;
      mockApi({
        [LIST]: () => page(++listed === 1 ? [account] : []),
        [`POST ${BASE}/${route}/${account.id}/token/`]: {
          status: 404,
          body: { code: "NOT_FOUND" },
        },
      });
      show();
      fireEvent.click(await screen.findByRole("button", { name: "Rotar token" }));
      await screen.findByRole("form", { name: "Rotar el token" });
      type(/^Token nuevo/, SECRET);
      fireEvent.click(screen.getByRole("button", { name: "Rotar" }));
      await waitFor(() => expect(screen.queryByText(account.name)).toBeNull()); // la lista nueva
      expect(screen.getByText("Esa cuenta ya no existe. Actualizamos la lista.")).toBeVisible();
      noSecret();
      cleanup();
      listed = 0;
      mockApi({
        [LIST]: () => page(++listed === 1 ? [account] : []),
        [`DELETE ${BASE}/${route}/${account.id}/`]: { status: 404, body: { code: "NOT_FOUND" } },
      });
      show();
      fireEvent.click(await screen.findByRole("button", { name: "Desconectar" }));
      const group = await screen.findByRole("group", { name: /^Desconectar / });
      fireEvent.click(within(group).getByRole("button", { name: "Sí, desconectar" }));
      await waitFor(() => expect(screen.queryByText(account.name)).toBeNull());
      expect(screen.getByText("Esa cuenta ya no existe. Actualizamos la lista.")).toBeVisible();
      expect(document.body.textContent).not.toMatch(/quedó desconectada/);
    },
  );

  it("una cuenta desconectada o de una plataforma desconocida no ofrece acciones", async () => {
    const gone = social("i4", { status: "DISCONNECTED" });
    const odd = social("x1", { platform: "TELEGRAM" } as unknown as Partial<ChannelAccount>);
    const proto = social("x2", { platform: "constructor" } as unknown as Partial<ChannelAccount>);
    mockApi({ [LIST]: page([gone, odd, proto]) });
    show();
    for (const card of await cards()) expect(within(card).queryByRole("button")).toBeNull();
  });
});

describe("Los códigos de error de canales", () => {
  // Cada código que la pantalla explica existe en el contrato (el esquema OpenAPI o los códigos
  // comunes del backend): si el backend lo renombra, esta prueba falla en vez de caer en el genérico.
  it("están en el contrato del backend", () => {
    const read = (path: string) =>
      readFileSync(fileURLToPath(new URL(path, import.meta.url)), "utf8");
    const contract =
      read("../../../../backend/openapi/schema.yaml") +
      read("../../../../backend/core/api/errors.py");
    for (const code of CHANNEL_CODES) expect(contract).toMatch(new RegExp(`\\b${code}\\b`));
  });

  it.each([
    [540, "RATE_LIMITED_WAIT:9"],
    [3600, "RATE_LIMITED_WAIT:60"],
    [1, "RATE_LIMITED_WAIT:1"],
    [0, "RATE_LIMITED"],
    [3601, "RATE_LIMITED"],
    [1.5, "RATE_LIMITED"],
    [Number.NaN, "RATE_LIMITED"],
    [undefined, "RATE_LIMITED"],
  ])("Retry-After %s solo se dice si es un entero de 1 s a una hora", (seconds, expected) => {
    const t = (key: string, values?: Record<string, number>) =>
      values ? `${key}:${values.minutes}` : key;
    const error = new ApiError(429, "RATE_LIMITED", undefined, seconds);
    expect(channelError(error, t)?.text).toBe(expected);
  });
});
