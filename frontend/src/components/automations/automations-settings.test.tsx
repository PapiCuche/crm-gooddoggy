import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AutomationsPage from "@/app/o/[orgSlug]/automatizaciones/page";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { AutomationRule, Automations, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

const BASE = "/api/v1/o/acme/automations";
const GET = `GET ${BASE}/`;
const PATCH = `PATCH ${BASE}/rules/conversation.unanswered/`;
const PAUSE = `POST ${BASE}/pause/`;
const RESUME = `POST ${BASE}/resume/`;
const DAY = 86_400_000;
const ADMIN = ["automations.view", "automations.manage", "automations.kill_switch"];
const SUPERVISOR = ["automations.view", "automations.kill_switch"];
const tenant = (codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  ...{ membership_id: "m1", mfa: { required: false, missing: false }, roles: [] },
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
// «Conversación sin responder» en SIMULATE desde hace `days` días (versión 3).
const simulating = (days: number): AutomationRule => {
  const since = Date.now() - days * DAY - 60_000;
  return {
    ...{ key: "conversation.unanswered", mode: "SIMULATE", version: 3, updated_at: null },
    params: { first_after_minutes: 15, escalate_after_minutes: 45, max_recipients: 3 },
    simulate_since: new Date(since).toISOString(),
    on_allowed_at: new Date(since + 7 * DAY).toISOString(),
  };
};
const QUOTE: AutomationRule = {
  ...{ key: "quote.no_response", mode: "OFF", version: 0, simulate_since: null },
  ...{ on_allowed_at: null, updated_at: null },
  params: { after_hours: 48, days: [1, 2, 3, 4, 5, 6], opens_at: "09:00", closes_at: "19:00" },
};
const state = (rule: AutomationRule, changes: Partial<Automations> = {}): Automations => ({
  ...{ global_enabled: true, paused: false, paused_changed_at: null, rules: [rule, QUOTE] },
  ...changes,
});
const PAUSED = { paused: true, paused_changed_at: "2026-11-27T15:00:00Z" };
const ok = (body: unknown) => ({ status: 200, body });
const fail = (status: number, code: string) => ({ status, body: { code, detail: "SERVIDOR" } });
const show = (codes: string[]) =>
  renderApp(
    <TenantProvider value={tenant(codes)}>
      <AutomationsPage />
    </TenantProvider>,
  );
const sent = (api: ReturnType<typeof mockApi>, route: string) =>
  api.mock.calls
    .filter(([to, init]) => `${init?.method ?? "GET"} ${String(to)}` === route)
    .map(([, init]) => (init?.body ? JSON.parse(String(init.body)) : null));
const card = (name: string) => screen.findByRole("region", { name });
const RULE = "Conversación sin responder";
const save = (rule: HTMLElement) =>
  fireEvent.click(within(rule).getByRole("button", { name: "Guardar cambios" }));

configure({ asyncUtilTimeout: 4000 });
beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Automatizaciones (F11-08a)", () => {
  it("Owner/Admin: modo y parámetros de cada regla, y el aviso del sistema apagado", async () => {
    const api = mockApi({ [GET]: ok(state(simulating(2), { global_enabled: false })) });
    show(ADMIN);
    expect(screen.getByText("Cargando las automatizaciones…")).toBeVisible();
    const rule = await card(RULE);
    expect(within(rule).getByRole("radio", { name: /^SIMULATE/ })).toBeChecked();
    expect(within(rule).getByLabelText(/Avisar tras/)).toHaveValue(15);
    expect(screen.getByText(/apagadas en todo el sistema/)).toBeVisible();
    const quote = await card("Cotización sin respuesta");
    expect(within(quote).getByRole("radio", { name: /^OFF/ })).toBeChecked();
    expect(within(quote).getByRole("checkbox", { name: /lun/i })).toBeChecked();
    expect(within(quote).getByRole("checkbox", { name: /dom/i })).not.toBeChecked();
    expect(within(quote).getByLabelText("Horario: desde")).toHaveValue("09:00");
    // Desde OFF, ON no se ofrece: primero SIMULATE.
    const on = within(quote).getByRole("radio", { name: /^ON/ });
    expect(on).toBeDisabled();
    expect(on).toHaveAccessibleDescription(/pásala primero a SIMULATE/);
    expect(screen.getByRole("button", { name: "Pausar todas" })).toBeVisible();
    // La pantalla solo lee la configuración: ni historial ni otros módulos.
    expect(api.mock.calls.map(([to]) => String(to))).toEqual([`${BASE}/`]);
  });

  it("ON no se puede elegir antes de 7 días en SIMULATE: dice cuánto falta", async () => {
    const api = mockApi({ [GET]: ok(state(simulating(2))) });
    show(ADMIN);
    const rule = await card(RULE);
    const on = within(rule).getByRole("radio", { name: /^ON/ });
    expect(on).toBeDisabled();
    expect(on).toHaveAccessibleDescription(/7 días seguidos.*propietario.*dentro de 5 días/);
    fireEvent.click(on);
    save(rule);
    expect(await within(rule).findByText("No hay cambios que guardar.")).toBeInTheDocument();
    expect(sent(api, PATCH)).toEqual([]);
  });

  it("tras 7 días, ON pide la revisión del propietario y enseña el rechazo de la API", async () => {
    const api = mockApi({
      [GET]: ok(state(simulating(8))),
      [PATCH]: fail(409, "SIMULATION_TOO_SHORT"),
    });
    show(ADMIN);
    const rule = await card(RULE);
    expect(within(rule).getByText(/Ya lleva 7 días en SIMULATE/)).toBeVisible();
    fireEvent.click(within(rule).getByRole("radio", { name: /^ON/ }));
    save(rule);
    expect(await within(rule).findByRole("alert")).toHaveTextContent(/revisión del propietario/);
    expect(document.activeElement).toBe(within(rule).getByRole("checkbox", { name: /revisó/ }));
    expect(sent(api, PATCH)).toEqual([]);
    fireEvent.click(within(rule).getByRole("checkbox", { name: /propietario revisó/ }));
    save(rule);
    expect(await within(rule).findByRole("alert")).toHaveTextContent(
      "La API rechazó ON: la regla todavía no lleva 7 días seguidos en SIMULATE. No cambió nada.",
    );
    expect(sent(api, PATCH)).toEqual([{ version: 3, mode: "ON" }]);
    expect(within(rule).getByRole("radio", { name: /^ON/ })).toBeChecked(); // lo elegido sigue
  });

  it("guardar envía solo lo que cambió; si otra persona la cambió, la relee", async () => {
    const saved = { ...simulating(2), version: 4, params: { first_after_minutes: 30 } };
    let stale = false;
    const api = mockApi({
      [GET]: ok(state(simulating(2))),
      [PATCH]: () => (stale ? fail(409, "STALE_VERSION") : ok(state(saved))),
    });
    show(ADMIN);
    const rule = await card(RULE);
    fireEvent.change(within(rule).getByLabelText(/Avisar tras/), { target: { value: "30" } });
    save(rule);
    expect(
      await within(rule).findByText("«Conversación sin responder» quedó guardada."),
    ).toBeVisible();
    expect(sent(api, PATCH)).toEqual([{ version: 3, params: { first_after_minutes: 30 } }]);
    expect(within(rule).getByLabelText(/Avisar tras/)).toHaveValue(30);
    stale = true;
    fireEvent.change(within(rule).getByLabelText(/Avisar tras/), { target: { value: "20" } });
    save(rule);
    expect(await within(rule).findByRole("alert")).toHaveTextContent("Otra persona cambió");
    expect(sent(api, PATCH).at(-1)).toEqual({ version: 4, params: { first_after_minutes: 20 } });
    await waitFor(() => expect(sent(api, GET)).toHaveLength(2));
  });

  it("un parámetro fuera de rango: la API lo rechaza y se dice sin repetir su texto", async () => {
    const api = mockApi({
      [GET]: ok(state(simulating(2))),
      [PATCH]: { status: 400, body: { code: "VALIDATION_ERROR", fields: { params: ["x"] } } },
    });
    show(ADMIN);
    const rule = await card(RULE);
    fireEvent.change(within(rule).getByLabelText(/Personas por aviso/), { target: { value: "9" } });
    save(rule);
    expect(await within(rule).findByRole("alert")).toHaveTextContent(
      "Algún parámetro está fuera de rango: revísalo.",
    );
    expect(sent(api, PATCH)).toEqual([{ version: 3, params: { max_recipients: 9 } }]);
  });

  it("Supervisor: lee y pausa; no edita ni reanuda", async () => {
    const api = mockApi({
      [GET]: ok(state(simulating(2))),
      [PAUSE]: ok({ global_enabled: true, ...PAUSED }),
    });
    show(SUPERVISOR);
    const rule = await card(RULE);
    expect(within(rule).queryByRole("radio")).toBeNull();
    expect(within(rule).queryByRole("textbox")).toBeNull();
    expect(within(rule).queryByRole("button")).toBeNull();
    expect(
      within(rule).getByText("Avisar tras (minutos sin respuesta)").nextSibling,
    ).toHaveTextContent("15");
    expect(within(rule).getByText(/dentro de 5 días/)).toBeVisible();
    expect(within(rule).getByText(/^Solo lectura/)).toBeVisible();
    const quote = await card("Cotización sin respuesta");
    expect(within(quote).getByText("Días de atención").nextSibling).toHaveTextContent(
      "lun, mar, mié, jue, vie, sáb",
    );
    fireEvent.click(screen.getByRole("button", { name: "Pausar todas" }));
    expect(await screen.findByText(/^Pausadas desde/)).toBeVisible();
    expect(screen.getByText("Las automatizaciones quedaron pausadas.")).toBeVisible();
    await waitFor(() => expect(document.activeElement).toHaveTextContent(/^Pausadas desde/));
    expect(screen.queryByRole("button", { name: "Reanudar" })).toBeNull();
    expect(screen.getByText(/Reanudarlas pide el permiso/)).toBeVisible();
    expect(sent(api, PAUSE)).toEqual([null]);
    expect(sent(api, PATCH)).toEqual([]);
  });

  it("Administrador: reanuda una organización pausada; un fallo se dice", async () => {
    let refused = true;
    const api = mockApi({
      [GET]: ok(state(simulating(2), PAUSED)),
      [RESUME]: () =>
        refused
          ? fail(403, "PERMISSION_DENIED")
          : ok({ global_enabled: true, paused: false, paused_changed_at: null }),
    });
    show(ADMIN);
    fireEvent.click(await screen.findByRole("button", { name: "Reanudar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para hacer esto.",
    );
    refused = false;
    fireEvent.click(screen.getByRole("button", { name: "Reanudar" }));
    expect(await screen.findByText("Las automatizaciones se reanudaron.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Pausar todas" })).toBeVisible();
    expect(sent(api, RESUME)).toEqual([null, null]);
  });

  it("sin `automations.view` no hay acceso ni peticiones; un 403 de la API, lo mismo", async () => {
    const api = mockApi({ [GET]: fail(403, "PERMISSION_DENIED") });
    show(["tasks.view"]);
    expect(screen.getByRole("alert")).toHaveTextContent("No tienes acceso a las automatizaciones.");
    expect(api).not.toHaveBeenCalled();
    cleanup();
    show(["automations.view"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("No tienes acceso");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("si la lectura falla, se dice y se puede reintentar", async () => {
    let down = true;
    mockApi({ [GET]: () => (down ? fail(500, "INTERNAL_ERROR") : ok(state(simulating(2)))) });
    show(ADMIN);
    expect(await screen.findByRole("alert")).toHaveTextContent("Algo salió mal de nuestro lado");
    down = false;
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await card("Cotización sin respuesta")).toBeVisible();
  });
});
