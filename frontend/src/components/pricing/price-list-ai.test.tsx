import { configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { PriceList, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { PriceLists } from "./price-lists";

const LIST = "GET /api/v1/o/acme/pricing/price-lists/";
const PUT = "PUT /api/v1/o/acme/pricing/price-lists/p1/ai-exposable/";
const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
const grants = (...codes: string[]) => codes.map((code) => ({ code, scopes: [] }));
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: grants("prices.view", ...codes),
});
const regular: PriceList = {
  ...{ id: "p1", code: "REGULAR", name: "Regular", kind: "RETAIL", currency: "PEN" },
  ...{ is_default: true, required_permission: "prices.view", ai_exposable: false },
  ...{ payment_condition: null, is_active: true, sort_order: 0 },
};
const shown = { ...regular, ai_exposable: true };
const staff: PriceList = {
  ...{ ...regular, id: "p2", code: "STAFF", name: "Staff", is_default: false },
  required_permission: "prices.view_internal",
};
const kindStaff: PriceList = { ...regular, id: "p3", code: "EQUIPO", kind: "INTERNAL" };
const page = (results: unknown[]) => ({ status: 200, body: { results, next: null } });
const fail = (status: number, code: string, wait?: string) => ({
  ...{ status, body: { code, message: "TEXTO DEL SERVIDOR" } },
  headers: wait ? { "Retry-After": wait } : undefined,
});
const show = (...codes: string[]) =>
  renderApp(
    <TenantProvider value={tenant(...codes)}>
      <PriceLists />
    </TenantProvider>,
  );
const sent = (api: ReturnType<typeof mockApi>, route: string) =>
  api.mock.calls
    .filter(([to, init]) => `${init?.method ?? "GET"} ${String(to)}` === route)
    .map(([, init]) => JSON.parse(String(init?.body)));
const box = (name = "Regular") =>
  screen.findByRole("checkbox", { name: `La IA puede citar los precios de ${name}` });
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
afterEach(() => vi.unstubAllGlobals());

describe("PriceListAi", () => {
  it("solo `ai.manage` ve el interruptor, nunca en la interna; los demás ven qué listas cita la IA", async () => {
    mockApi({ [LIST]: page([shown, staff, kindStaff]) });
    const view = show("prices.manage");
    const [first] = await screen.findAllByRole("listitem");
    expect(within(first!).getByText("La IA puede citarla")).toBeVisible();
    expect(screen.queryByRole("checkbox")).toBeNull(); // `prices.manage` no basta
    press("Editar la lista Regular", first!);
    expect(within(screen.getByRole("form")).queryByText(/IA/)).toBeNull(); // ya no se edita aquí
    view.unmount();
    show("ai.manage");
    expect(await box()).toBeChecked();
    expect(await box()).toHaveAccessibleDescription(/segundo factor/);
    expect(screen.getAllByRole("checkbox")).toHaveLength(1); // ni STAFF ni EQUIPO
    expect(screen.queryByRole("button", { name: /Editar/ })).toBeNull();
  });

  it("marcar pide el código si la API lo pide, repite una vez y dice lo hecho", async () => {
    let rows = [regular];
    let tried = 0;
    const api = mockApi({
      [LIST]: () => page(rows),
      [PUT]: () => {
        if (++tried === 1) return fail(403, "STEP_UP_REQUIRED");
        rows = [shown];
        return { status: 200, body: shown };
      },
      [STEP_UP]: { status: 200, body: { step_up_expires_in: 900 } },
    });
    show("ai.manage");
    fireEvent.click(await box());
    await verify();
    expect(await screen.findByText("La IA ya puede citar los precios de «Regular».")).toHaveRole(
      "status",
    );
    expect(await box()).toBeChecked();
    expect(sent(api, PUT)).toEqual([{ ai_exposable: true }, { ai_exposable: true }]);
    expect(sent(api, STEP_UP)).toHaveLength(1);
  });

  it("desmarcar envía `false`; cancelar el código no cambia nada ni lo dice", async () => {
    const api = mockApi({ [LIST]: page([shown]), [PUT]: fail(403, "STEP_UP_REQUIRED") });
    show("ai.manage");
    fireEvent.click(await box());
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(sent(api, PUT)).toEqual([{ ai_exposable: false }]);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(await box()).toBeChecked();
  });

  it.each([
    [fail(403, "PERMISSION_DENIED"), "No tienes permiso para decidir qué listas cita la IA."],
    [fail(429, "RATE_LIMITED", "120"), "Demasiados intentos. Vuelve a probar en 2 minutos."],
    [fail(400, "VALIDATION_ERROR"), "La lista interna nunca la cita la IA."],
    [fail(500, "INTERNAL_ERROR"), "No se pudo cambiar si la IA cita «Regular»."],
  ])("los errores van por `code`, sin el texto del servidor (%#)", async (reply, text) => {
    mockApi({ [LIST]: page([regular]), [PUT]: reply });
    show("ai.manage");
    fireEvent.click(await box());
    expect(await screen.findByRole("alert")).toHaveTextContent(text);
    expect(screen.queryByText(/TEXTO DEL SERVIDOR/)).toBeNull();
    expect(await box()).not.toBeChecked();
  });

  it("si la lista ya no existe (404), lo dice la vista y la vuelve a pedir", async () => {
    let rows = [regular];
    mockApi({
      [LIST]: () => page(rows),
      [PUT]: () => {
        rows = [];
        return fail(404, "NOT_FOUND");
      },
    });
    show("ai.manage");
    fireEvent.click(await box());
    expect(await screen.findByText(/La lista «Regular» ya no existe/)).toBeVisible();
    expect(await screen.findByText(/todavía no tiene listas/)).toBeVisible();
  });
});

function press(name: string, inside: HTMLElement) {
  fireEvent.click(within(inside).getByRole("button", { name }));
}
