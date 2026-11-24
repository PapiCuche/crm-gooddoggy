import { cleanup, configure, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantGate } from "@/components/app-shell/tenant-gate";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import { WorkspaceHome } from "@/components/app-shell/workspace-home";
import type { ChannelAccount, Conversation, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { notifyNewMessage } from "./attention";
import { Inbox } from "./inbox";

vi.mock("next/navigation", async () => ({
  useSearchParams: (await import("@/test-search-params")).useSearchParams,
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/o/acme/inbox",
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

const BASE = "GET /api/v1/o/acme/conversations/";
const COUNT = `${BASE}unanswered/`;
const CHANNELS = "GET /api/v1/o/acme/channels/?limit=200";
const tenant = (codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const ok = (body: unknown) => ({ status: 200, body });
const page = (results: unknown[]) => ok({ results, next: null });
const rosa = {
  ...{ id: "1", number: "CONV-00001", status: "OPEN", attention_mode: "HUMAN" },
  ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
  ...{ contact: { id: "c1", display_name: "Rosa Díaz" } },
  ...{
    channel: {
      id: "ch1",
      platform: "WHATSAPP",
      name: "Tienda Lima",
      text_max: 4096,
      text_unit: "CHARACTERS",
      accepts_media: true,
    },
  },
  ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: "2026-11-04T15:30:00Z" },
  ...{ last_message_preview: "Hola", last_message_direction: "INBOUND", version: 1 },
} as Conversation;
const account = (id: string, changes: Partial<ChannelAccount>) =>
  ({
    id,
    platform: "WHATSAPP",
    name: `Cuenta ${id}`,
    status: "CONNECTED",
    status_detail: null,
    ...changes,
  }) as ChannelAccount;
const show = (codes = ["conversations.view"]) =>
  renderApp(
    <TenantProvider value={tenant(codes)}>
      <Inbox />
    </TenantProvider>,
  );

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("Inbox para atender rápido (F6-09)", () => {
  it("«Sin responder» filtra en el servidor y cuenta en la bandeja elegida", async () => {
    const api = mockApi({
      [BASE]: page([rosa, { ...rosa, id: "2", channel: { ...rosa.channel, platform: "SANDBOX" } }]),
      [`${BASE}?unanswered=true`]: page([rosa]),
      [COUNT]: ok({ count: 1 }),
      [`${COUNT}?box=mine`]: ok({ count: 0 }),
      [`${BASE}?box=mine&unanswered=true`]: page([]),
      [`${BASE}1/`]: ok(rosa),
      [`${BASE}1/messages/`]: page([]),
    });
    show();
    const filter = await screen.findByRole("button", {
      name: "Sin responder, 1 conversación sin responder",
    });
    expect(filter).toHaveAttribute("aria-pressed", "false");
    const rows = within(await screen.findByRole("list", { name: /conversaciones/i }));
    expect(rows.getAllByRole("button")).toHaveLength(2);
    expect(rows.getAllByText("WhatsApp")).toHaveLength(1); // la red de cada fila, en texto
    expect(rows.getByText("Pruebas")).toBeVisible();
    fireEvent.click(filter);
    const list = async () => within(await screen.findByRole("list", { name: /conversaciones/i }));
    await waitFor(async () => expect((await list()).getAllByRole("button")).toHaveLength(1));
    expect(filter).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Mías" }));
    expect(await screen.findByText("Ninguna conversación coincide con los filtros.")).toBeVisible();
    expect(screen.getByRole("button", { name: /^Sin responder/ })).toHaveAccessibleName(
      "Sin responder, 0 conversaciones sin responder",
    );
    expect(api).toHaveBeenCalledWith(
      `/api/v1/o/acme/conversations/?box=mine&unanswered=true`,
      expect.anything(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Todas" }));
    fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
    const header = (await screen.findByRole("heading", { level: 2 })).closest("header")!;
    expect(within(header).getByText("WhatsApp")).toBeVisible(); // y en el chat
  });

  it("el título de la pestaña y el menú cuentan las que esperan; sin permiso no se pregunta", async () => {
    mockApi({
      "GET /api/v1/o/acme/me/": ok(tenant(["conversations.view"])),
      [COUNT]: ok({ count: 3 }),
    });
    renderApp(
      <TenantGate orgSlug="acme">
        <WorkspaceHome />
      </TenantGate>,
    );
    const nav = await screen.findByRole("navigation", { name: "Navegación principal" });
    await waitFor(() => expect(document.title).toBe("(3) Inbox · Acme SAC · Good Doggy CRM"));
    expect(within(nav).getByRole("link", { name: /Inbox/ })).toHaveAccessibleName(
      "Inbox, 3 conversaciones sin responder",
    );
    cleanup();
    const api = mockApi({ "GET /api/v1/o/acme/me/": ok(tenant(["users.view"])) });
    renderApp(
      <TenantGate orgSlug="acme">
        <WorkspaceHome />
      </TenantGate>,
    );
    await screen.findByRole("navigation", { name: "Navegación principal" });
    expect(document.title).toBe("Inbox · Acme SAC · Good Doggy CRM"); // sin número
    expect(api).toHaveBeenCalledTimes(1); // solo `me`: nada del Inbox
  });

  it("avisa de las cuentas que no pueden atender a quien puede arreglarlas", async () => {
    const accounts = [
      account("a", { status: "ERROR", status_detail: "TOKEN_INVALID" }),
      account("b", { status: "DISCONNECTED" }),
      account("c", { status: "PENDING", status_detail: "WEBHOOK_NOT_SEEN" }),
      account("d", {}),
      account("e", { platform: "SANDBOX", status: "PENDING" }),
    ];
    let api = mockApi({ [BASE]: page([]), [CHANNELS]: page(accounts) });
    show(["conversations.view", "channels.manage"]);
    const banner = await screen.findByRole("region", { name: "Cuentas de canal con problemas" });
    expect(
      within(banner)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual([
      "Cuenta a: el token venció o ya no es válido; no se pueden enviar mensajes.",
      "Cuenta b: está desconectada y no recibe ni envía mensajes.",
      "Cuenta c: falta terminar la conexión o aprobar permisos.",
    ]);
    expect(within(banner).getByRole("link", { name: "Revisar en Canales" })).toHaveAttribute(
      "href",
      "/o/acme/canales",
    );
    cleanup();
    api = mockApi({ [BASE]: page([]), [CHANNELS]: page(accounts) });
    show();
    await screen.findByText("Todavía no hay conversaciones.");
    expect(screen.queryByRole("region", { name: /Cuentas de canal/ })).not.toBeInTheDocument();
    expect(api).not.toHaveBeenCalledWith(expect.stringContaining("/channels/"), expect.anything());
  });

  it("con el chat abierto en 390 px el aviso no lo empuja: se oculta y sigue en la lista", async () => {
    const down = page([account("a", { status: "DISCONNECTED" })]);
    const chat = { [`${BASE}1/`]: ok(rosa), [`${BASE}1/messages/`]: page([]) };
    mockApi({ [BASE]: page([rosa]), ...chat, [CHANNELS]: down });
    show(["conversations.view", "channels.manage"]);
    const banner = await screen.findByRole("region", { name: "Cuentas de canal con problemas" });
    expect(banner.parentElement).not.toHaveClass("max-lg:hidden");
    fireEvent.click(await screen.findByRole("button", { name: /Rosa Díaz/ }));
    await screen.findByRole("heading", { level: 2 });
    expect(banner.parentElement).toHaveClass("max-lg:hidden");
  });

  it("el aviso del navegador se pide con un clic y solo dice «Nuevo mensaje»", async () => {
    const sent: unknown[][] = [];
    const Fake = vi.fn(function (...args: unknown[]) {
      sent.push(args);
    }) as unknown as typeof Notification & { permission: string };
    Object.assign(Fake, {
      permission: "default",
      requestPermission: vi.fn(async () => (Fake.permission = "granted")),
    });
    vi.stubGlobal("Notification", Fake);
    const hidden = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    mockApi({ [BASE]: page([]) });
    show();
    const toggle = await screen.findByRole("button", { name: "Avisarme de mensajes nuevos" });
    expect(Fake.requestPermission).not.toHaveBeenCalled(); // nunca al cargar
    notifyNewMessage("Nuevo mensaje");
    expect(sent).toEqual([]); // apagado
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "true"));
    expect(Fake.requestPermission).toHaveBeenCalledTimes(1);
    notifyNewMessage("Nuevo mensaje");
    expect(sent).toEqual([["Nuevo mensaje", { tag: "inbox.notify", renotify: true }]]); // sin nada del cliente
    const focus = vi.spyOn(window, "focus").mockImplementation(() => {});
    const [shown] = vi.mocked(Fake).mock.instances as unknown as { onclick?: () => void }[];
    shown!.onclick?.();
    expect(focus).toHaveBeenCalledOnce(); // al pulsarlo se vuelve a la pestaña
    hidden.mockReturnValue("visible");
    notifyNewMessage("Nuevo mensaje");
    expect(sent).toHaveLength(1); // con la pestaña a la vista no hace falta
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).toHaveAttribute("aria-pressed", "false"));
    hidden.mockReturnValue("hidden");
    notifyNewMessage("Nuevo mensaje");
    expect(sent).toHaveLength(1);
    hidden.mockRestore();
  });
});
