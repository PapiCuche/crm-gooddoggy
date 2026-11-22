import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { AiBudget, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { AiBudgetPanel } from "./ai-budget";
import { AiNav } from "./ai-nav";

vi.mock("next/navigation", () => ({ usePathname: () => "/o/acme/ia/presupuesto" }));

const ROUTE = "/api/v1/o/acme/ai/budget/";
const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
const tenant = (codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  ...{ membership_id: "m1", mfa: { required: false, missing: false }, roles: [] },
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const caps = (monthly: string | null, daily: string | null): AiBudget => ({
  ...{ monthly_usd: monthly, daily_usd: daily },
});
const SET = caps("50.000000", "5.000000");
const ok = (body: unknown) => ({ status: 200, body });
const fail = (status: number, code: string, fields?: unknown, wait?: string) => ({
  ...{ status, body: { code, message: "TEXTO DEL SERVIDOR", fields } },
  headers: wait ? { "Retry-After": wait } : undefined,
});
const STEP = fail(403, "STEP_UP_REQUIRED");
const show = (codes = ["ai.manage"]) =>
  renderApp(
    <TenantProvider value={tenant(codes)}>
      <AiNav />
      <AiBudgetPanel />
    </TenantProvider>,
  );
const sent = (api: ReturnType<typeof mockApi>, method = "GET") =>
  api.mock.calls
    .filter(([to, init]) => String(to) === ROUTE && (init?.method ?? "GET") === method)
    .map(([, init]) => (init?.body ? JSON.parse(String(init.body)) : null));
const monthly = () => screen.findByLabelText<HTMLInputElement>("Tope mensual (USD)");
const daily = () => screen.findByLabelText<HTMLInputElement>("Tope diario (USD)");
const write = async (month: string | null, day: string | null) => {
  if (month !== null) fireEvent.change(await monthly(), { target: { value: month } });
  if (day !== null) fireEvent.change(await daily(), { target: { value: day } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar topes" }));
};
const verify = async () => {
  const dialog = await screen.findByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText(/^Código/), { target: { value: "123456" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Verificar" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
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
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Presupuesto de IA (F8-12a)", () => {
  it("muestra los topes guardados en USD, como se escribirían, y la pestaña", async () => {
    const api = mockApi({ [`GET ${ROUTE}`]: ok(caps("50.000000", "0.125000")) });
    show();
    expect(screen.getByText("Cargando el presupuesto de IA…")).toBeVisible();
    expect((await monthly()).value).toBe("50");
    expect((await daily()).value).toBe("0.125");
    expect(screen.getByText(/^Vigentes: .*50\.00 al mes y .*0\.125 al día\.$/)).toBeVisible();
    expect(screen.getByText(/^Lo gastado no se muestra aquí/)).toBeVisible();
    const tabs = screen.getByRole("navigation", { name: "Secciones de inteligencia artificial" });
    expect(within(tabs).getByRole("link", { name: "Presupuesto" })).toHaveAttribute(
      ...["href", "/o/acme/ia/presupuesto"],
    );
    expect(sent(api)).toHaveLength(1);
  });

  it("sin topes lo dice; guardar envía los dos por PUT y enseña lo guardado sin otra lectura", async () => {
    const api = mockApi({
      [`GET ${ROUTE}`]: ok(caps(null, null)),
      [`PUT ${ROUTE}`]: ok(caps("30.000000", "2.500000")),
    });
    show();
    expect(await screen.findByText(/^Todavía no hay topes/)).toBeVisible();
    expect((await monthly()).value).toBe("");
    await write(" 30 ", "2.5");
    expect(await screen.findByText(/^Los topes quedaron guardados/)).toBeVisible();
    expect(sent(api, "PUT")).toEqual([{ monthly_usd: "30", daily_usd: "2.5" }]);
    expect(screen.getByText(/^Vigentes: .*30\.00 al mes y .*2\.50 al día\.$/)).toBeVisible();
    expect(sent(api)).toHaveLength(1);
  });

  it.each([
    ["el diario por encima del mensual", "10", "10.000001", "daily", "El tope diario no puede"],
    ["más de 6 decimales", "1.1234567", "1", "monthly", "Usa 6 decimales como mucho."],
    ["una coma", "1,5", "1", "monthly", "Escribe solo números, con punto"],
    ["un negativo", "-5", "1", "monthly", "Escribe solo números, con punto"],
    ["cero", "50", "0.000000", "daily", "Debe ser mayor que cero."],
    ["un millón", "1000000", "1", "monthly", "Debe ser menor que un millón"],
    ["vacío", "50", "", "daily", "Escribe un importe."],
  ])("%s se explica junto al campo y no se envía", async (_, month, day, bad, text) => {
    const api = mockApi({ [`GET ${ROUTE}`]: ok(SET) });
    show();
    await write(month, day);
    const field = await (bad === "daily" ? daily() : monthly());
    await waitFor(() => expect(field).toHaveAttribute("aria-invalid", "true"));
    expect(field).toHaveAccessibleDescription(expect.stringContaining(text));
    expect(field).toHaveFocus();
    expect(sent(api, "PUT")).toHaveLength(0);
  });

  it.each([
    [{ daily_usd: [{ code: "invalid" }] }, "Tope diario (USD)", "No se aceptó este importe"],
    [{ monthly_usd: [{ code: "max_decimal_places" }] }, "Tope mensual (USD)", "Usa 6 decimales"],
    [{ monthly_usd: [{ code: "min_value" }] }, "Tope mensual (USD)", "Debe ser mayor que cero."],
    [{ daily_usd: [{ code: "max_whole_digits" }] }, "Tope diario (USD)", "menor que un millón"],
  ])(
    "un 400 de la API %j se dice por su código, nunca con su texto",
    async (fields, label, text) => {
      mockApi({
        [`GET ${ROUTE}`]: ok(SET),
        [`PUT ${ROUTE}`]: fail(400, "VALIDATION_ERROR", fields),
      });
      show();
      await write("60", null);
      const field = await screen.findByLabelText(label);
      await waitFor(() => expect(field).toHaveAccessibleDescription(expect.stringContaining(text)));
      expect(document.body.textContent).not.toContain("SERVIDOR");
    },
  );

  it("el mismo importe con otra forma no se envía; descartar vuelve a lo guardado", async () => {
    const api = mockApi({ [`GET ${ROUTE}`]: ok(SET) });
    show();
    await write("50.00", "5.0");
    expect(await screen.findByText("No hay cambios que guardar.")).toBeVisible();
    fireEvent.change(await monthly(), { target: { value: "70" } });
    fireEvent.click(screen.getByRole("button", { name: "Descartar cambios" }));
    expect((await monthly()).value).toBe("50");
    expect(sent(api, "PUT")).toHaveLength(0);
  });

  it("mientras se guarda: campos de solo lectura y un envío; si falla, lo escrito se queda", async () => {
    let finish: (reply: Response) => void = () => {};
    const api = mockApi({ [`GET ${ROUTE}`]: ok(SET) });
    api.mockImplementation(async (to, init) =>
      init?.method === "PUT"
        ? new Promise<Response>((resolve) => (finish = resolve))
        : new Response(JSON.stringify(SET), { status: 200 }),
    );
    show();
    await write("80", "8");
    const button = screen.getByRole("button", { name: "Guardando…" });
    expect(await monthly()).toHaveAttribute("readonly");
    expect(await daily()).toHaveAttribute("readonly");
    expect(button).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(button);
    fireEvent.submit(button.closest("form")!);
    expect(sent(api, "PUT")).toHaveLength(1);
    finish(new Response(JSON.stringify({ code: "INTERNAL_ERROR" }), { status: 500 }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    expect((await monthly()).value).toBe("80");
    expect((await daily()).value).toBe("8");
    expect(await monthly()).not.toHaveAttribute("readonly");
  });

  it("leer sin el segundo factor abre el código y, verificado, enseña los topes", async () => {
    let reads = 0;
    mockApi({
      [`GET ${ROUTE}`]: () => (++reads === 1 ? STEP : ok(SET)),
      [STEP_UP]: ok({ step_up_expires_in: 900 }),
    });
    show();
    expect(await screen.findByText(/^Para ver o cambiar los topes/)).toBeVisible();
    await verify();
    expect((await monthly()).value).toBe("50");
    expect(reads).toBe(2);
  });

  it("guardar sin el segundo factor pide el código y repite una vez con los mismos importes", async () => {
    let puts = 0;
    const api = mockApi({
      [`GET ${ROUTE}`]: ok(SET),
      [`PUT ${ROUTE}`]: () => (++puts === 1 ? STEP : ok(caps("90.000000", "9.000000"))),
      [STEP_UP]: ok({ step_up_expires_in: 900 }),
    });
    show();
    await write("90", "9");
    await verify();
    expect(await screen.findByText(/^Los topes quedaron guardados/)).toBeVisible();
    expect(sent(api, "PUT")).toEqual([
      { monthly_usd: "90", daily_usd: "9" },
      { monthly_usd: "90", daily_usd: "9" },
    ]);
  });

  it.each([
    [fail(429, "RATE_LIMITED", undefined, "120"), "Vuelve a probar en 2 minutos."],
    [fail(403, "PERMISSION_DENIED"), "No tienes permiso para gestionar la IA."],
  ])("guardar: %j se dice con nuestro texto", async (reply, text) => {
    mockApi({ [`GET ${ROUTE}`]: ok(SET), [`PUT ${ROUTE}`]: reply });
    show();
    await write("60", null);
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect((await monthly()).value).toBe("60");
  });

  it("sin `ai.manage` (Supervisor, Vendedor) lo explica, sin pestañas ni llamadas", async () => {
    const api = mockApi({});
    show(["ai.kill_switch"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("Solo quien tiene el permiso");
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByLabelText("Tope mensual (USD)")).toBeNull();
    expect(api).not.toHaveBeenCalled();
  });
});
