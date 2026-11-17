import { focusManager } from "@tanstack/react-query";
import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { AiProviderAccount, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { browser } from "./ai-key-actions";
import { AiProviderAccounts } from "./ai-provider-accounts";

const BASE = "/api/v1/o/acme/ai/provider-accounts";
const LIST = `GET ${BASE}/`;
const CREATE = `POST ${BASE}/`;
const TEST = `POST ${BASE}/k1/test/`;
const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
const PASTED = "fake-provider-key-for-ui-tests-0001"; // ficticia, con la forma que acepta la API
const tenant = (codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  ...{ membership_id: "m1", mfa: { required: false, missing: false }, roles: [] },
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const account = (changes: Partial<AiProviderAccount> = {}): AiProviderAccount => ({
  ...{ id: "k1", provider: "ANTHROPIC", status: "ACTIVE", key_last_four: "0001" },
  ...{ configured_at: "2026-11-10T15:00:00Z", rotated_at: null, revoked_at: null },
  ...{ updated_at: "2026-11-10T15:00:00Z", ...changes },
});
const ok = (body: unknown, status = 200) => ({ status, body });
const fail = (status: number, code: string, wait?: string) => ({
  ...{
    status,
    body: { code, detail: "TEXTO DEL SERVIDOR", fields: { api_key: ["TEXTO DEL CAMPO"] } },
  },
  headers: wait ? { "Retry-After": wait } : undefined,
});
const STEP = fail(403, "STEP_UP_REQUIRED");
const show = (codes = ["ai.manage"]) =>
  renderApp(
    <TenantProvider value={tenant(codes)}>
      <AiProviderAccounts />
    </TenantProvider>,
  );
const sent = (api: ReturnType<typeof mockApi>, method = "GET", url = `${BASE}/`) =>
  api.mock.calls
    .filter(([to, init]) => String(to) === url && (init?.method ?? "GET") === method)
    .map(([, init]) => (init?.body ? JSON.parse(String(init.body)) : null));
const keyField = () => screen.getByLabelText(/^Clave de API/) as HTMLInputElement;
const openForm = async (button: string, form: string) => {
  fireEvent.click(await screen.findByRole("button", { name: button }));
  return screen.findByRole("form", { name: form });
};
const saveForm = () => openForm("Guardar clave de Anthropic", "Guardar la clave de Anthropic");
const paste = (submit: string) => {
  fireEvent.change(keyField(), { target: { value: PASTED } });
  fireEvent.click(screen.getByRole("button", { name: submit }));
};
const verify = async () => {
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText(/^Código/), { target: { value: "123456" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Verificar" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
};
// La clave no queda en el DOM (texto y atributos), el almacenamiento, la URL ni las cachés.
const nowhere = ({ client }: ReturnType<typeof renderApp>) => {
  expect(new XMLSerializer().serializeToString(document.body)).not.toContain(PASTED);
  expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain(PASTED);
  expect(location.href).not.toContain(PASTED);
  const cached = [...client.getMutationCache().getAll(), ...client.getQueryCache().getAll()];
  expect(JSON.stringify(cached.map((entry) => entry.state))).not.toContain(PASTED);
};

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
// En cada test la consola se observa: la clave nunca sale por ella.
let logged: ReturnType<typeof vi.spyOn>[] = [];
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
  logged = (["log", "info", "warn", "error", "debug"] as const).map((level) =>
    vi.spyOn(console, level),
  );
});
afterEach(() => {
  for (const spy of logged) expect(JSON.stringify(spy.mock.calls)).not.toContain(PASTED);
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Proveedor de IA (F8-04)", () => {
  it("lista la cuenta con su estado, sus fechas y solo los últimos cuatro de la clave", async () => {
    const api = mockApi({ [LIST]: ok([account({ rotated_at: "2026-11-12T10:00:00Z" })]) });
    show();
    expect(screen.getByText("Cargando el proveedor de IA…")).toBeVisible();
    const list = await screen.findByRole("list", { name: "Proveedor de IA" });
    const item = within(list).getByRole("listitem");
    for (const text of ["Anthropic", "Activa", "…0001", "Guardada", "Rotada"]) {
      expect(item).toHaveTextContent(text);
    }
    expect(item).not.toHaveTextContent("Borrada");
    expect(
      within(item)
        .getAllByRole("button")
        .map((b) => b.textContent),
    ).toEqual(["Probar conexión"]);
    // con una clave activa no se ofrece guardar otra (rotarla llega con F8-04b)
    expect(screen.queryByRole("button", { name: "Guardar clave de Anthropic" })).toBeNull();
    expect(sent(api)).toHaveLength(1);
  });

  it("sin `ai.manage` lo explica y no llama a la API (el tope cuenta también los 403)", async () => {
    const api = mockApi({ [LIST]: ok([account()]) });
    show(["channels.manage"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("Solo quien tiene el permiso");
    expect(api).not.toHaveBeenCalled();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it.each([
    [fail(403, "PERMISSION_DENIED"), "Solo quien tiene el permiso de gestionar la IA"],
    [fail(429, "RATE_LIMITED", "61"), "Demasiados intentos. Vuelve a probar en 2 minutos."],
    [fail(404, "NOT_FOUND"), "No encontramos el proveedor de IA de esta organización."],
  ])("un error de la lista se explica por su código, sin ofrecer acciones", async (reply, text) => {
    mockApi({ [LIST]: reply });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByRole("button", { name: /clave|conexión/ })).toBeNull();
  });

  it("la lista se pide una vez: ni al recuperar el foco o la red ni al reintentar sola un 500", async () => {
    const api = mockApi({ [LIST]: fail(500, "INTERNAL_ERROR") });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect(sent(api)).toHaveLength(1);
    const again = mockApi({ [LIST]: ok([account()]) });
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" })); // solo a mano
    expect(await screen.findByText("…0001")).toBeVisible();
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    window.dispatchEvent(new Event("online"));
    await new Promise((done) => setTimeout(done, 50));
    expect(sent(again)).toHaveLength(1);
    focusManager.setFocused(undefined);
  });

  it("STEP_UP_REQUIRED en la lista abre el código; cancelarlo lo explica y se reabre sin otra lectura", async () => {
    let listed = 0;
    const api = mockApi({
      [LIST]: () => (++listed === 1 ? STEP : ok([account()])),
      [STEP_UP]: ok({ step_up_expires_in: 900 }),
    });
    show();
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("alert")).toHaveTextContent("confirma tu identidad con un código");
    fireEvent.click(screen.getByRole("button", { name: "Confirmar con mi código" }));
    expect(sent(api)).toHaveLength(1);
    await verify();
    expect(await screen.findByText("…0001")).toBeVisible();
    expect(sent(api)).toHaveLength(2);
  });

  it("guardar: contraseña por POST, un envío, el campo vacío y la clave en ninguna parte", async () => {
    const api = mockApi({ [LIST]: ok([]), [CREATE]: ok(account(), 201) });
    const view = show();
    expect(await screen.findByText("Todavía no hay ninguna clave guardada.")).toBeVisible();
    const form = await saveForm();
    expect(form).toHaveAttribute("method", "post"); // sin JS nunca sería un GET con la clave
    const field = keyField();
    expect(field).toHaveAttribute("type", "password");
    expect(field).toHaveAttribute("autocomplete", "new-password");
    expect(field).toHaveAttribute("spellcheck", "false");
    paste("Guardar");
    fireEvent.submit(form); // la segunda pulsación no es otra escritura
    expect(field.value).toBe("");
    expect(await screen.findByText(/La clave quedó guardada/)).toBeVisible();
    expect(screen.getByText("…0001")).toBeVisible();
    expect(sent(api, "POST")).toEqual([{ provider: "ANTHROPIC", api_key: PASTED }]);
    expect(api.mock.calls.filter(([url]) => String(url).includes(PASTED))).toEqual([]);
    expect(sent(api)).toHaveLength(1); // la cuenta llega en la respuesta: no se gasta otra lectura
    nowhere(view);
  });

  it("guardar con STEP_UP_REQUIRED abre el código y repite una sola vez con la misma clave", async () => {
    let posts = 0;
    const api = mockApi({
      [LIST]: ok([]),
      [CREATE]: () => (++posts === 1 ? STEP : ok(account(), 201)),
      [STEP_UP]: ok({ step_up_expires_in: 900 }),
    });
    const view = show();
    await saveForm();
    paste("Guardar");
    await verify();
    expect(await screen.findByText(/La clave quedó guardada/)).toBeVisible();
    expect(sent(api, "POST").map((body) => body.api_key)).toEqual([PASTED, PASTED]);
    nowhere(view);
  });

  it.each([
    [
      fail(409, "AI_ACCOUNT_EXISTS"),
      "Ya hay una clave guardada para ese proveedor. Actualizamos",
      2,
    ],
    [fail(409, "AI_ACCOUNT_CONFLICT"), "Otra persona está guardando la clave ahora mismo.", 1],
    [fail(403, "PERMISSION_DENIED"), "No tienes permiso para gestionar la IA.", 1],
    [fail(429, "RATE_LIMITED", "540"), "Vuelve a probar en 9 minutos.", 1],
    [fail(429, "RATE_LIMITED", "999999999"), "Espera un momento antes de volver a probar.", 1],
    [fail(400, "VALIDATION_ERROR"), "Eso no tiene el formato de una clave de API.", 1],
    [fail(500, "CODIGO_NUEVO"), "Algo salió mal de nuestro lado", 1],
  ])("guardar: %o se explica por su código", async (reply, text, lists) => {
    const api = mockApi({ [LIST]: ok([]), [CREATE]: reply });
    const view = show();
    await saveForm();
    paste("Guardar");
    expect(await screen.findByText(new RegExp(text))).toBeVisible();
    for (const raw of ["TEXTO DEL SERVIDOR", "TEXTO DEL CAMPO", reply.body.code]) {
      expect(document.body.textContent).not.toContain(raw);
    }
    await waitFor(() => expect(sent(api)).toHaveLength(lists)); // «ya existe» vuelve a leer
    nowhere(view);
  });

  it("guardar: un 401 lleva al login sin enseñar un error", async () => {
    const go = vi.spyOn(browser, "assign").mockImplementation(() => {});
    mockApi({ [LIST]: ok([]), [CREATE]: fail(401, "NOT_AUTHENTICATED") });
    show();
    await saveForm();
    paste("Guardar");
    await waitFor(() => expect(go).toHaveBeenCalledWith(expect.stringMatching(/^\/login\b/)));
    expect(go).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("guardar sobre una cuenta borrada la reactiva en su sitio, sin otra lectura", async () => {
    const off = account({ status: "DISABLED", revoked_at: "2026-11-12T10:00:00Z" });
    const back = account({ key_last_four: "0003", configured_at: "2026-11-17T09:00:00Z" });
    const api = mockApi({ [LIST]: ok([off]), [CREATE]: ok(back, 201) });
    show();
    expect(await screen.findByText("Borrada", { selector: "p" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Probar conexión" })).toBeNull();
    await saveForm();
    paste("Guardar");
    expect(await screen.findByText("…0003")).toBeVisible();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Probar conexión" })).toBeVisible();
    expect(sent(api)).toHaveLength(1);
  });

  it.each([
    ["OK", "Conexión correcta: el proveedor aceptó la clave."],
    ["CREDENTIALS_REJECTED", "El proveedor no aceptó la clave. Revisa en su consola"],
    ["PROVIDER_BILLING", "El proveedor rechazó la llamada por facturación"],
    ["PROVIDER_UNAVAILABLE", "El proveedor no responde ahora."],
    ["RESULTADO_NUEVO", "El resultado de la prueba no se pudo interpretar."],
  ])(
    "probar la conexión: %s se dice con su texto, una llamada por pulsación",
    async (result, text) => {
      const api = mockApi({ [LIST]: ok([account()]), [TEST]: ok({ result }) });
      show();
      const button = await screen.findByRole("button", { name: "Probar conexión" });
      fireEvent.click(button);
      fireEvent.click(button);
      expect(await screen.findByText(new RegExp(text))).toBeVisible();
      expect(document.body.textContent).not.toContain("RESULTADO_NUEVO");
      expect(sent(api, "POST", `${BASE}/k1/test/`)).toHaveLength(1);
    },
  );

  it("probar la conexión: STEP_UP_REQUIRED pide el código y 429 dice cuánto esperar", async () => {
    let tests = 0;
    mockApi({
      [LIST]: ok([account()]),
      [TEST]: () => (++tests === 1 ? STEP : fail(429, "RATE_LIMITED", "120")),
      [STEP_UP]: ok({ step_up_expires_in: 900 }),
    });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Probar conexión" }));
    await verify();
    expect(await screen.findByRole("alert")).toHaveTextContent("Vuelve a probar en 2 minutos.");
    expect(tests).toBe(2);
  });
});
