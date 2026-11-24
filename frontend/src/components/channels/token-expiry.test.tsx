import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { ChannelAccount, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ChannelsList } from "./channels-list";

// La caducidad del token en la tarjeta de Messenger e Instagram (F7b-12b, #1098). Las horas, en
// la zona de `renderApp` (Lima), con el reloj del navegador fijo en `NOW`.
const LIST = "GET /api/v1/o/acme/channels/";
const NOW = Date.parse("2026-11-24T12:00:00Z");
const DAY = 24 * 60 * 60 * 1000;
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: [{ code: "channels.manage", scopes: [] }],
};
const account = (id: string, changes: Partial<ChannelAccount>) =>
  ({
    ...{ id, platform: "MESSENGER", name: `Página ${id}`, status: "CONNECTED" },
    ...{ status_detail: null, quality_rating: null, messaging_limit_tier: null },
    ...{ last_healthcheck_at: null, external_account_id: "100000000000101" },
    ...{ external_parent_id: null, token_last_four: "WXYZ", page_subscription: "UNKNOWN" },
    ...{ token_expiry: "UNKNOWN", token_expires_at: null, has_standby_conversations: false },
    ...{ version: 1, created_at: "2026-11-01T10:00:00Z", updated_at: "2026-11-01T10:00:00Z" },
    ...changes,
  }) as ChannelAccount;
const at = (ms: number) =>
  ({ token_expiry: "AT", token_expires_at: new Date(NOW + ms).toISOString() }) as const;

// La fila «Caduca» de cada tarjeta, en el orden de la lista; `null` si no la tiene.
async function rows(accounts: ChannelAccount[]) {
  mockApi({ [LIST]: { status: 200, body: { results: accounts, next: null } } });
  renderApp(
    <TenantProvider value={tenant}>
      <ChannelsList />
    </TenantProvider>,
  );
  const list = await screen.findByRole("list", { name: "Canales" });
  return [...list.querySelectorAll(":scope > li")].map((card) => {
    const label = within(card as HTMLElement).queryByText("Caduca", { selector: "dt" });
    return label ? (label.nextElementSibling as HTMLElement) : null;
  });
}
const red = (cell: HTMLElement | null | undefined) =>
  Boolean(cell?.querySelector(".text-danger")) && Boolean(cell?.querySelector("svg"));

beforeEach(() => vi.useFakeTimers({ toFake: ["Date"], now: NOW }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("La caducidad del token en Canales (F7b-12b)", () => {
  it("dice los tres estados: la fecha, que no caduca y que no se sabe", async () => {
    const [dated, never, unknown] = await rows([
      account("m1", at(30 * DAY)),
      account("i1", {
        ...{ platform: "INSTAGRAM", token_expiry: "NEVER" },
        ...{ external_account_id: "100000000000102", external_parent_id: "100000000000101" },
      }),
      account("m2", { token_expiry: "UNKNOWN" }),
    ]);
    expect(dated).toHaveTextContent(/^24 dic\. 2026, 07:00 a\. m\.$/);
    expect(red(dated)).toBe(false);
    expect(never).toHaveTextContent(/^No caduca, según Meta$/);
    expect(unknown).toHaveTextContent(/^No se sabe todavía$/);
    expect([never, unknown].some(red)).toBe(false);
  });

  it("en rojo y con icono a 10 días justos o menos; un minuto más, sin marcar", async () => {
    const [edge, inside, outside] = await rows([
      account("m1", at(10 * DAY)),
      account("m2", at(DAY)),
      account("m3", at(10 * DAY + 60_000)),
    ]);
    expect(edge).toHaveTextContent(/^4 dic\. 2026, 07:00 a\. m\. \(pronto\)$/);
    expect([edge, inside].every(red)).toBe(true);
    expect(outside).toHaveTextContent(/^4 dic\. 2026, 07:01 a\. m\.$/);
    expect(red(outside)).toBe(false);
  });

  it("una fecha pasada se dice caducada, en rojo y con icono, no como si viniera", async () => {
    const [gone, now] = await rows([account("m1", at(-2 * DAY)), account("m2", at(0))]);
    expect(gone).toHaveTextContent(/^Caducó el 22 nov\. 2026, 07:00 a\. m\.$/);
    expect(now).toHaveTextContent(/^Caducó el 24 nov\. 2026, 07:00 a\. m\.$/);
    expect([gone, now].every(red)).toBe(true);
  });

  it("sin credencial viva, en WhatsApp o en la Sandbox no hay fila; un `AT` sin fecha no se inventa", async () => {
    const found = await rows([
      account("m1", { token_expiry: null, token_expires_at: null, token_last_four: null }),
      account("w1", {
        ...{ platform: "WHATSAPP", token_expiry: "UNKNOWN", quality_rating: "GREEN" },
        ...{ external_account_id: "200000000000001", external_parent_id: "300000000000001" },
      }),
      account("s1", { platform: "SANDBOX", token_expiry: null, token_last_four: null }),
      account("m2", { token_expiry: "AT", token_expires_at: null }),
    ]);
    expect(found.slice(0, 3)).toEqual([null, null, null]);
    expect(found[3]).toHaveTextContent(/^No se sabe todavía$/);
  });
});
