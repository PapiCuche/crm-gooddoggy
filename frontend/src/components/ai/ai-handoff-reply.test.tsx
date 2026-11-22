import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { AiHandoffReply, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { AiHandoffReplyPanel } from "./ai-handoff-reply";
import { AiNav } from "./ai-nav";

vi.mock("next/navigation", () => ({ usePathname: () => "/o/acme/ia/traspaso" }));

const URL = "/api/v1/o/acme/ai/handoff-reply/";
const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
const INITIAL = "Entiendo. Voy a solicitar que una persona de nuestro equipo continúe contigo.";
const MINE = "Gracias por escribirnos. Una persona del equipo seguirá esta conversación contigo.";
const tenant = (codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  ...{ membership_id: "m1", mfa: { required: false, missing: false }, roles: [] },
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const reply = (changes: Partial<AiHandoffReply> = {}): AiHandoffReply => ({
  ...{ text: INITIAL, initial: true, changed_at: null, ...changes },
});
const ok = (body: unknown) => ({ status: 200, body });
const fail = (status: number, code: string, fields?: unknown, wait?: string) => ({
  ...{ status, body: { code, message: "TEXTO DEL SERVIDOR", fields } },
  headers: wait ? { "Retry-After": wait } : undefined,
});
const invalid = (...codes: string[]) =>
  fail(400, "VALIDATION_ERROR", { text: codes.map((code) => ({ code, message: "DEL SERVIDOR" })) });
const STEP = fail(403, "STEP_UP_REQUIRED");
const show = (codes = ["ai.manage"]) =>
  renderApp(
    <TenantProvider value={tenant(codes)}>
      <AiNav />
      <AiHandoffReplyPanel />
    </TenantProvider>,
  );
const sent = (api: ReturnType<typeof mockApi>, method = "GET") =>
  api.mock.calls
    .filter(([to, init]) => String(to) === URL && (init?.method ?? "GET") === method)
    .map(([, init]) => (init?.body ? JSON.parse(String(init.body)) : null));
const field = () => screen.findByRole("textbox", { name: "Texto que propone el copiloto" });
const write = async (text: string) => {
  fireEvent.change(await field(), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar texto" }));
};
const verify = async () => {
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText(/^Código/), { target: { value: "123456" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Verificar" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
};
// Lo que describe el campo: la nota, el contador y, si los hay, sus problemas.
const described = (element: HTMLElement) =>
  (element.getAttribute("aria-describedby") ?? "")
    .split(" ")
    .map((id) => document.getElementById(id)?.textContent);

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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Texto de traspaso (F8-09c)", () => {
  it("muestra el texto inicial, su contador y la nota de no prometer atención inmediata", async () => {
    const api = mockApi({ [`GET ${URL}`]: ok(reply()) });
    show();
    expect(screen.getByText("Cargando el texto de traspaso…")).toBeVisible();
    const area = (await field()) as HTMLTextAreaElement;
    expect(area.value).toBe(INITIAL);
    expect(screen.getByText("Texto inicial")).toBeVisible();
    const [note, counter] = described(area);
    expect(note).toMatch(/^Nunca prometas atención inmediata ni rápida/);
    expect(counter).toBe(`${Array.from(INITIAL).length} de 500 caracteres`);
    expect(area).not.toHaveAttribute("aria-invalid");
    const tabs = screen.getByRole("navigation", { name: "Secciones de inteligencia artificial" });
    expect(
      within(tabs)
        .getAllByRole("link")
        .map((link) => link.getAttribute("href")),
    ).toEqual(["/o/acme/ia", "/o/acme/ia/traspaso"]);
    expect(sent(api)).toHaveLength(1);
  });

  it("guardar envía el texto sin los blancos de los bordes por PUT y pasa a «Texto propio»", async () => {
    const saved = reply({ text: MINE, initial: false, changed_at: "2026-11-22T15:00:00Z" });
    const api = mockApi({ [`GET ${URL}`]: ok(reply()), [`PUT ${URL}`]: ok(saved) });
    show();
    await write(`  ${MINE}\n`);
    expect(await screen.findByText(/El texto quedó guardado/)).toBeVisible();
    expect(sent(api, "PUT")).toEqual([{ text: MINE }]);
    expect(screen.getByText("Texto propio")).toBeVisible();
    expect(((await field()) as HTMLTextAreaElement).value).toBe(MINE);
    expect(screen.getByText(/Último cambio:/)).toBeVisible();
    expect(sent(api)).toHaveLength(1); // el guardado llega en la respuesta: no se vuelve a leer
  });

  it("guardar el mismo texto propio no escribe; descartar vuelve al guardado", async () => {
    const mine = reply({ text: MINE, initial: false, changed_at: "2026-11-22T15:00:00Z" });
    const api = mockApi({ [`GET ${URL}`]: ok(mine) });
    show();
    await write(MINE);
    expect(await screen.findByText("No hay cambios que guardar.")).toBeVisible();
    fireEvent.change(await field(), { target: { value: "Otro" } });
    fireEvent.click(screen.getByRole("button", { name: "Descartar cambios" }));
    expect(((await field()) as HTMLTextAreaElement).value).toBe(MINE);
    expect(screen.queryByRole("button", { name: "Descartar cambios" })).toBeNull();
    expect(sent(api, "PUT")).toEqual([]);
  });

  it("un texto guardado que ya no vale se dice: el copiloto usa el inicial", async () => {
    mockApi({ [`GET ${URL}`]: ok(reply({ changed_at: "2026-11-20T10:00:00Z" })) });
    show();
    expect(await screen.findByText(/El texto guardado ya no cumple las reglas/)).toBeVisible();
    expect(screen.getByText("Texto inicial")).toBeVisible();
  });

  it.each([
    ["SOON", "No prometas atención inmediata ni rápida"],
    ["BODY", "Usa solo texto visible, sin caracteres ocultos ni de control."],
    ["TOO_LONG", "Usa como mucho 500 caracteres."],
    ["SCRIPT", "Usa solo letras del alfabeto latino."],
    ["FIGURE", "Quita las cifras, importes y monedas."],
    ["DELIVERY", "Quita los plazos, fechas, horarios y entregas."],
    ["DISCOUNT", "Quita los descuentos, ofertas y comparaciones de precio."],
    ["AVAILABILITY", "No hables de stock ni de disponibilidad de productos."],
    ["TERMS", "Quita las condiciones de pago, garantías, cambios y devoluciones."],
    ["LINK", "Quita los enlaces, dominios y correos."],
    ["MARKUP", "Quita las llaves y la marca [verificar]."],
    ["blank", "Escribe el texto de traspaso."],
    ["CODIGO_NUEVO", "El texto lleva algo que el copiloto no puede proponer."],
  ])("un 400 con `%s` se dice en español, en el campo y con el foco en él", async (code, text) => {
    mockApi({ [`GET ${URL}`]: ok(reply()), [`PUT ${URL}`]: invalid(code) });
    show();
    await write("Te atendemos enseguida.");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(text);
    const area = await field();
    expect(area).toHaveAttribute("aria-invalid", "true");
    expect(area).toHaveFocus();
    expect(described(area)[2]).toContain(text);
    expect(document.body).not.toHaveTextContent("SERVIDOR");
  });

  it("varios problemas a la vez, uno por línea y sin repetir", async () => {
    const bad = invalid("SOON", "LINK", "OTRO", "NUEVO");
    mockApi({ [`GET ${URL}`]: ok(reply()), [`PUT ${URL}`]: bad });
    show();
    await write("Ya te atendemos, mira tienda.pe");
    const items = within(await screen.findByRole("alert")).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringMatching(/^No prometas atención inmediata/),
      "Quita los enlaces, dominios y correos.",
      "El texto lleva algo que el copiloto no puede proponer. Revísalo.",
    ]);
  });

  it("vacío o de más de 500 caracteres no se envía", async () => {
    const api = mockApi({ [`GET ${URL}`]: ok(reply()) });
    show();
    await write("   ");
    expect(await screen.findByRole("alert")).toHaveTextContent("Escribe el texto de traspaso.");
    expect(await field()).toHaveFocus();
    await write("a".repeat(501));
    expect(await screen.findByRole("alert")).toHaveTextContent("Usa como mucho 500 caracteres.");
    expect(screen.getByText("501 de 500 caracteres: sobran 1")).toBeVisible();
    fireEvent.change(await field(), { target: { value: "🐶".repeat(500) } }); // uno por emoji
    expect(screen.getByText("500 de 500 caracteres")).toBeVisible();
    expect(sent(api, "PUT")).toEqual([]);
  });

  it("STEP_UP_REQUIRED al leer abre el código; al verificarlo se vuelve a leer", async () => {
    let reads = 0;
    const api = mockApi({
      [`GET ${URL}`]: () => (++reads === 1 ? STEP : ok(reply())),
      [STEP_UP]: ok({ step_up_expires_in: 900 }),
    });
    show();
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("alert")).toHaveTextContent("confirma tu identidad con un código");
    fireEvent.click(screen.getByRole("button", { name: "Confirmar con mi código" }));
    await verify();
    expect(((await field()) as HTMLTextAreaElement).value).toBe(INITIAL);
    expect(sent(api)).toHaveLength(2);
  });

  it("guardar con STEP_UP_REQUIRED pide el código y repite una sola vez con el mismo texto", async () => {
    let puts = 0;
    const saved = reply({ text: MINE, initial: false, changed_at: "2026-11-22T15:00:00Z" });
    const api = mockApi({
      [`GET ${URL}`]: ok(reply()),
      [`PUT ${URL}`]: () => (++puts === 1 ? STEP : ok(saved)),
      [STEP_UP]: ok({ step_up_expires_in: 900 }),
    });
    show();
    await write(MINE);
    await verify();
    expect(await screen.findByText(/El texto quedó guardado/)).toBeVisible();
    expect(sent(api, "PUT")).toEqual([{ text: MINE }, { text: MINE }]);
  });

  it.each([
    [fail(429, "RATE_LIMITED", undefined, "540"), "Vuelve a probar en 9 minutos."],
    [fail(429, "RATE_LIMITED"), "Espera un momento antes de volver a probar."],
    [fail(403, "PERMISSION_DENIED"), "No tienes permiso para gestionar la IA."],
    [fail(500, "CODIGO_NUEVO"), "Algo salió mal de nuestro lado"],
  ])("un fallo al guardar se dice por su código (%#)", async (answer, text) => {
    mockApi({ [`GET ${URL}`]: ok(reply()), [`PUT ${URL}`]: answer });
    show();
    await write(MINE);
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(await field()).not.toHaveAttribute("aria-invalid");
    expect(screen.getByRole("button", { name: "Guardar texto" })).toBeVisible();
    expect(document.body).not.toHaveTextContent("SERVIDOR");
  });

  it.each([[["ai.use", "ai.kill_switch"]], [[]]])(
    "sin `ai.manage` (%j) no hay pestañas ni lectura: solo el aviso",
    async (codes) => {
      const api = mockApi({ [`GET ${URL}`]: ok(reply()) });
      show(codes);
      expect(screen.getByRole("alert")).toHaveTextContent("Solo quien tiene el permiso");
      expect(screen.queryByRole("navigation")).toBeNull();
      expect(screen.queryByRole("textbox")).toBeNull();
      await new Promise((done) => setTimeout(done, 50));
      expect(api).not.toHaveBeenCalled();
    },
  );
});
