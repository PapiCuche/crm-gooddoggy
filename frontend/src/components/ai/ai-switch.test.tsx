import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import AiPage from "@/app/o/[orgSlug]/ia/page";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { AiSwitch, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { browser } from "./ai-key-actions";

const BASE = "/api/v1/o/acme/ai";
const GET = `GET ${BASE}/switch/`;
const OFF = `POST ${BASE}/switch/off/`;
const ON = `POST ${BASE}/switch/on/`;
const LIST_URL = `${BASE}/provider-accounts/`;
const LIST = `GET ${LIST_URL}`;
const STEP_UP = "POST /api/v1/auth/mfa/step-up/";
const MANAGE = ["ai.manage"];
const OWNER = ["ai.manage", "ai.kill_switch"];
const KILL = ["ai.kill_switch"];
const tenant = (codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  ...{ membership_id: "m1", mfa: { required: false, missing: false }, roles: [] },
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const state = (changes: Partial<AiSwitch> = {}): AiSwitch => ({
  ...{ enabled: true, global_enabled: true, changed_at: "2026-11-18T15:00:00Z", ...changes },
});
const ok = (body: unknown) => ({ status: 200, body });
const fail = (status: number, code: string, wait?: string) => ({
  ...{ status, body: { code, detail: "TEXTO DEL SERVIDOR" } },
  headers: wait ? { "Retry-After": wait } : undefined,
});
const STEP = fail(403, "STEP_UP_REQUIRED");
const show = (codes: string[]) =>
  renderApp(
    <TenantProvider value={tenant(codes)}>
      <AiPage />
    </TenantProvider>,
  );
const calls = (api: ReturnType<typeof mockApi>, route: string) =>
  api.mock.calls
    .filter(([to, init]) => `${init?.method ?? "GET"} ${String(to)}` === route)
    .map(([, init]) => (init?.body ? JSON.parse(String(init.body)) : null));
const panel = () => screen.findByRole("region", { name: "Estado de la IA" });
const press = async (name: string) =>
  fireEvent.click(await within(await panel()).findByRole("button", { name }));
const turnOn = async (confirm = true) => {
  await press("Encender la IA");
  const group = screen.getByRole("group", { name: "Encender la IA" });
  if (confirm) fireEvent.click(within(group).getByRole("checkbox"));
  fireEvent.click(within(group).getByRole("button", { name: "Sí, encender" }));
  return group;
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

describe("Interruptor de la IA (F8-06b)", () => {
  it("muestra los dos interruptores y el último cambio; mientras carga lo dice", async () => {
    mockApi({ [GET]: ok(state()), [LIST]: ok([]) });
    show(MANAGE);
    expect(screen.getByText("Cargando el estado de la IA…")).toBeVisible();
    const region = await panel();
    expect(await within(region).findByText("La IA está encendida.")).toBeVisible();
    const value = (term: string) => within(region).getByText(term).nextElementSibling;
    expect(value("Esta organización")).toHaveTextContent("Encendida");
    expect(value("Todo el sistema")).toHaveTextContent("Encendida");
    expect(region.querySelector("time")).toHaveAttribute("dateTime", "2026-11-18T15:00:00Z");
    expect(region.querySelector("time")).toHaveTextContent(/18.*nov.*2026/);
    expect(within(region).queryByText(/apagada en todo el sistema/)).toBeNull();
  });

  it("apagar: un POST sin cuerpo ni segundo factor, una sola vez, y lo que queda lo dice", async () => {
    const api = mockApi({
      [GET]: ok(state()),
      [LIST]: ok([]),
      [OFF]: ok(state({ enabled: false, changed_at: "2026-11-19T10:00:00Z" })),
    });
    show(MANAGE);
    const region = await panel();
    const button = await within(region).findByRole("button", { name: "Apagar la IA" });
    fireEvent.click(button);
    fireEvent.click(button); // la segunda pulsación no es otra petición
    expect(await within(region).findByText("La IA está apagada.")).toBeVisible();
    expect(within(region).getByRole("status")).toHaveTextContent(
      "La IA quedó apagada en esta organización.",
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(within(region).getByRole("heading", { level: 2 })),
    );
    expect(calls(api, OFF)).toEqual([null]);
    expect(calls(api, STEP_UP)).toEqual([]);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(within(region).queryByRole("button", { name: "Apagar la IA" })).toBeNull();
    expect(within(region).getByRole("button", { name: "Encender la IA" })).toBeVisible();
  });

  it.each([
    ["cargando", "pending"],
    ["con error", "error"],
    ["pidiendo el código", "step"],
  ])(
    "apagar no espera a la lista de cuentas (%s) ni abre el segundo factor al llegar",
    async (_, list) => {
      const api = mockApi({
        [GET]: ok(state()),
        [LIST]: list === "step" ? STEP : fail(500, "INTERNAL_ERROR"),
        [OFF]: ok(state({ enabled: false })),
      });
      if (list === "pending") {
        const real = api.getMockImplementation()!;
        api.mockImplementation((input, init) =>
          String(input) === LIST_URL ? new Promise<Response>(() => {}) : real(input, init),
        );
      }
      show(MANAGE);
      await press("Apagar la IA");
      expect(await screen.findByText("La IA quedó apagada en esta organización.")).toBeVisible();
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(calls(api, OFF)).toHaveLength(1);
      if (list === "step") {
        // el código de la lista se pide a mano: abre el diálogo y vuelve a leer
        fireEvent.click(await screen.findByRole("button", { name: "Confirmar con mi código" }));
        expect(await screen.findByRole("dialog")).toBeDefined();
      }
    },
  );

  it("solo `ai.kill_switch`: apaga, no ofrece encender y no pide la lista ni el código", async () => {
    const api = mockApi({ [GET]: ok(state()), [OFF]: ok(state({ enabled: false })) });
    show(KILL);
    const region = await panel();
    expect(
      screen.getByText(
        "La clave y el proveedor de IA los gestiona quien tiene el permiso de gestionar la IA.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
    await press("Apagar la IA");
    expect(await within(region).findByText("La IA está apagada.")).toBeVisible();
    expect(within(region).queryByRole("button", { name: "Encender la IA" })).toBeNull();
    expect(
      within(region).getByText("Para encenderla hace falta el permiso de gestionar la IA."),
    ).toBeVisible();
    expect(calls(api, LIST)).toEqual([]);
    expect(calls(api, STEP_UP)).toEqual([]);
    expect(api.mock.calls.map(([to]) => String(to))).not.toContain(LIST_URL);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("sin ninguno de los dos permisos no se pide el interruptor", async () => {
    const api = mockApi({ [GET]: ok(state()) });
    show(["channels.manage"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("Solo quien tiene el permiso");
    expect(screen.queryByRole("region", { name: "Estado de la IA" })).toBeNull();
    expect(api).not.toHaveBeenCalled();
  });

  it("encender: pide la confirmación del aviso, después el código, y repite con `notice_confirmed`", async () => {
    let tried = 0;
    const api = mockApi({
      [GET]: ok(state({ enabled: false, changed_at: null })),
      [LIST]: ok([]),
      [ON]: () => (++tried === 1 ? STEP : ok(state())),
      [STEP_UP]: ok({ step_up_expires_in: 900 }),
    });
    show(MANAGE);
    const region = await panel();
    expect(await within(region).findByText("Nunca")).toBeVisible();
    const group = await turnOn(false);
    expect(within(group).getByRole("alert")).toHaveTextContent(
      "Marca la confirmación del aviso de datos",
    );
    expect(calls(api, ON)).toEqual([]);
    expect(within(group).getByRole("checkbox")).toHaveAccessibleName(
      /aviso de tratamiento de datos está publicado/,
    );
    fireEvent.click(within(group).getByRole("checkbox"));
    fireEvent.click(within(group).getByRole("button", { name: "Sí, encender" }));
    await verify();
    expect(await within(region).findByText("La IA está encendida.")).toBeVisible();
    expect(within(region).getByText("La IA quedó encendida en esta organización.")).toBeVisible();
    expect(calls(api, ON)).toEqual([{ notice_confirmed: true }, { notice_confirmed: true }]);
    expect(within(region).getByRole("button", { name: "Apagar la IA" })).toBeVisible();
  });

  it("cancelar el código no es un fallo y cancelar el aviso no envía nada", async () => {
    const api = mockApi({
      [GET]: ok(state({ enabled: false })),
      [LIST]: ok([]),
      [ON]: STEP,
    });
    show(MANAGE);
    const group = await turnOn();
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(within(group).queryByRole("alert")).toBeNull();
    expect(calls(api, ON)).toHaveLength(1);
    fireEvent.click(within(group).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("group", { name: "Encender la IA" })).toBeNull();
    expect(calls(api, ON)).toHaveLength(1);
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Encender la IA" })),
    );
  });

  it("con `AI_ENABLED` apagado lo dice, cuenta la IA como apagada y no lo deja cambiar", async () => {
    const api = mockApi({
      [GET]: ok(state({ global_enabled: false })),
      [LIST]: ok([]),
      [OFF]: ok(state({ enabled: false, global_enabled: false })),
      [ON]: ok(state({ global_enabled: false })),
    });
    show(MANAGE);
    const region = await panel();
    expect(await within(region).findByText("La IA está apagada.")).toBeVisible();
    expect(within(region).getByText(/apagada en todo el sistema/)).toBeVisible();
    expect(within(region).getByText("Todo el sistema").nextElementSibling).toHaveTextContent(
      "Apagada",
    );
    expect(within(region).queryByRole("button", { name: /sistema|servidor/ })).toBeNull();
    // la organización sí se apaga y se enciende: queda lista para cuando el servidor la encienda
    await press("Apagar la IA");
    expect(await within(region).findByText(/quedó apagada/)).toBeVisible();
    await turnOn();
    expect(
      await within(region).findByText(
        "Esta organización quedó encendida, pero la IA sigue apagada hasta que se encienda en el servidor.",
      ),
    ).toBeVisible();
    expect(within(region).getByText("La IA está apagada.")).toBeVisible();
    expect(calls(api, OFF)).toHaveLength(1);
  });

  it.each([
    [fail(403, "PERMISSION_DENIED"), "No tienes permiso para ver el estado de la IA."],
    [fail(500, "INTERNAL_ERROR"), "Algo salió mal de nuestro lado"],
  ])("un error al leer se explica y se reintenta a mano", async (reply, text) => {
    mockApi({ [GET]: reply, [LIST]: ok([]) });
    show(KILL);
    const region = await panel();
    expect(await within(region).findByRole("alert")).toHaveTextContent(text);
    const again = mockApi({ [GET]: ok(state()) });
    fireEvent.click(within(region).getByRole("button", { name: "Reintentar" }));
    expect(await within(region).findByText("La IA está encendida.")).toBeVisible();
    expect(calls(again, GET)).toHaveLength(1);
  });

  it("sin poder leer el estado, apagar sigue a mano (sin el permiso, no)", async () => {
    const api = mockApi({
      [GET]: fail(500, "INTERNAL_ERROR"),
      [OFF]: ok(state({ enabled: false })),
    });
    show(KILL);
    await press("Apagar la IA");
    const region = await panel();
    expect(await within(region).findByText("La IA está apagada.")).toBeVisible();
    expect(calls(api, OFF)).toHaveLength(1);
    cleanup();
    mockApi({ [GET]: fail(403, "PERMISSION_DENIED") });
    show(KILL);
    expect(await within(await panel()).findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Apagar la IA" })).toBeNull();
  });

  it("un 401 al leer no se pinta como error (el shell lleva al login); al escribir, al login", async () => {
    const go = vi.spyOn(browser, "assign").mockImplementation(() => {});
    mockApi({ [GET]: fail(401, "NOT_AUTHENTICATED") });
    show(KILL);
    const region = await panel();
    await waitFor(() =>
      expect(within(region).getByText("Cargando el estado de la IA…")).toBeVisible(),
    );
    await new Promise((done) => setTimeout(done, 50));
    expect(within(region).queryByRole("alert")).toBeNull();
    expect(within(region).queryByRole("button")).toBeNull();
    cleanup();
    mockApi({ [GET]: ok(state()), [OFF]: fail(401, "NOT_AUTHENTICATED") });
    show(KILL);
    await press("Apagar la IA");
    await waitFor(() => expect(go).toHaveBeenCalledWith(expect.stringContaining("/login")));
    expect(within(await panel()).queryByRole("alert")).toBeNull();
  });

  it("una lectura en vuelo no pisa lo que devolvió apagar", async () => {
    let release: (value: Response) => void = () => {};
    let read = 0;
    const api = mockApi({ [LIST]: ok([]), [OFF]: ok(state({ enabled: false })) });
    const real = api.getMockImplementation()!;
    api.mockImplementation((input, init) => {
      if (`${init?.method ?? "GET"} ${String(input)}` !== GET) return real(input, init);
      if (++read === 1) return Promise.resolve(new Response(JSON.stringify(state())));
      return new Promise<Response>((done) => (release = done)); // la del foco, retenida
    });
    const view = show(OWNER);
    const region = await panel();
    expect(await within(region).findByText("La IA está encendida.")).toBeVisible();
    void view.client.refetchQueries({ queryKey: [`${BASE}/switch/`] });
    await waitFor(() => expect(read).toBe(2));
    await press("Apagar la IA");
    expect(await within(region).findByText("La IA está apagada.")).toBeVisible();
    release(new Response(JSON.stringify(state()))); // llega tarde, con el estado de antes
    await new Promise((done) => setTimeout(done, 50));
    expect(within(region).getByText("La IA está apagada.")).toBeVisible();
  });

  it("el Owner (los dos permisos) ve las cuentas, sin la nota, y puede encender", async () => {
    const api = mockApi({ [GET]: ok(state({ enabled: false })), [LIST]: ok([]) });
    show(OWNER);
    expect(await screen.findByText("Todavía no hay ninguna clave guardada.")).toBeVisible();
    expect(screen.queryByText(/los gestiona quien tiene el permiso/)).toBeNull();
    expect(
      await within(await panel()).findByRole("button", { name: "Encender la IA" }),
    ).toBeVisible();
    expect(calls(api, LIST)).toHaveLength(1);
  });

  it("el código de encender también desbloquea la lista de cuentas, sin pedir otro", async () => {
    let listed = 0;
    let tried = 0;
    const api = mockApi({
      [GET]: ok(state({ enabled: false })),
      [LIST]: () => (++listed === 1 ? STEP : ok([])),
      [ON]: () => (++tried === 1 ? STEP : ok(state())),
      [STEP_UP]: ok({ step_up_expires_in: 900 }),
    });
    show(OWNER);
    expect(await screen.findByRole("button", { name: "Confirmar con mi código" })).toBeVisible();
    await turnOn();
    await verify();
    expect(await screen.findByText("Todavía no hay ninguna clave guardada.")).toBeVisible();
    expect(calls(api, LIST)).toHaveLength(2);
    expect(calls(api, STEP_UP)).toHaveLength(1);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("con el sistema y la organización apagados lo dicen las dos filas y el aviso", async () => {
    mockApi({ [GET]: ok(state({ enabled: false, global_enabled: false })), [LIST]: ok([]) });
    show(MANAGE);
    const region = await panel();
    expect(await within(region).findByText(/apagada en todo el sistema/)).toBeVisible();
    for (const term of ["Esta organización", "Todo el sistema"]) {
      expect(within(region).getByText(term).nextElementSibling).toHaveTextContent("Apagada");
    }
  });

  it("apagar con STEP_UP_REQUIRED lo dice sin abrir el código; el aviso se va al reintentar", async () => {
    let release: (value: Response) => void = () => {};
    let tried = 0;
    const api = mockApi({ [GET]: ok(state()), [LIST]: ok([]), [OFF]: STEP });
    const real = api.getMockImplementation()!;
    api.mockImplementation((input, init) =>
      `${init?.method} ${String(input)}` === OFF && ++tried === 2
        ? new Promise<Response>((done) => (release = done)) // la segunda, retenida
        : real(input, init),
    );
    show(MANAGE);
    await press("Apagar la IA");
    const region = await panel();
    expect(await within(region).findByRole("alert")).toHaveTextContent("Confirma tu identidad");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls(api, STEP_UP)).toEqual([]);
    await press("Apagar la IA");
    // mientras la nueva petición está en vuelo, el aviso de la anterior ya no está
    expect(await within(region).findByRole("button", { name: "Apagando…" })).toBeVisible();
    expect(within(region).queryByRole("alert")).toBeNull();
    release(new Response(JSON.stringify(state({ enabled: false }))));
    expect(await within(region).findByText("La IA está apagada.")).toBeVisible();
    expect(within(region).queryByRole("alert")).toBeNull();
  });

  it.each([
    [OFF, true, fail(500, "INTERNAL_ERROR"), "No se pudo cambiar el estado de la IA."],
    [OFF, true, fail(404, "NOT_FOUND"), "No se pudo cambiar el estado de la IA."],
    [OFF, true, fail(403, "PERMISSION_DENIED"), "No tienes permiso para cambiar el estado"],
    [ON, false, fail(429, "RATE_LIMITED", "61"), "Vuelve a probar en 2 minutos."],
    [ON, false, fail(404, "NOT_FOUND"), "No se pudo cambiar el estado de la IA."],
    [ON, false, fail(403, "PERMISSION_DENIED"), "No tienes permiso para cambiar el estado"],
  ])(
    "un error de %s se explica por su código y el estado no cambia",
    async (route, on, reply, text) => {
      mockApi({ [GET]: ok(state({ enabled: on })), [LIST]: ok([]), [route]: reply });
      show(MANAGE);
      const region = await panel();
      if (on) await press("Apagar la IA");
      else await turnOn();
      expect(await within(region).findByRole("alert")).toHaveTextContent(text);
      expect(within(region).queryByText("TEXTO DEL SERVIDOR")).toBeNull();
      expect(within(region).queryByText(/clave|gestionar/)).toBeNull();
      expect(
        within(region).getByText(on ? "La IA está encendida." : "La IA está apagada."),
      ).toBeVisible();
    },
  );
});
