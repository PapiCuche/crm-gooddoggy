import {
  act,
  cleanup,
  configure,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Conversation, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { Inbox } from "./inbox";

vi.mock("next/navigation", async () => ({
  useSearchParams: (await import("@/test-search-params")).useSearchParams,
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/o/acme/inbox",
}));

const LIST = "GET /api/v1/o/acme/conversations/";
const COUNT = `${LIST}unanswered/`;
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: [{ code: "conversations.view", scopes: [] }],
};
const ok = (body: unknown) => ({ status: 200, body });
const page = (results: unknown[], next: string | null = null) => ok({ results, next });
const row = (id: string, name: string, platform: string) =>
  ({
    ...{ id, number: `CONV-0000${id}`, status: "OPEN", attention_mode: "HUMAN" },
    ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
    ...{ contact: { id: `c${id}`, display_name: name } },
    ...{ channel: { id: `ch-${platform}`, platform, name: `Cuenta ${platform}` } },
    ...{ last_message_at: "2026-11-04T15:30:00Z", last_inbound_at: "2026-11-04T15:30:00Z" },
    ...{ last_message_preview: "Hola", last_message_direction: "INBOUND", version: 1 },
  }) as unknown as Conversation;
const rosa = row("1", "Rosa Díaz", "WHATSAPP");
const ines = row("2", "Inés Soto", "INSTAGRAM");
const show = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <Inbox />
    </TenantProvider>,
  );
const asked = (api: ReturnType<typeof mockApi>) => api.mock.calls.map(([url]) => String(url));
const chips = () => within(screen.getByRole("group", { name: "Red" }));
const OPTIONS = `${LIST}accounts/?limit=200`;
const LIMA = { id: "ch-WHATSAPP", platform: "WHATSAPP", name: "Ventas Lima" };
const IG = { id: "ch-INSTAGRAM", platform: "INSTAGRAM", name: "Tienda IG" };
const picker = () => screen.findByRole("combobox", { name: "Cuenta" });

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("filtro por red del Inbox (UX-INBOX-02)", () => {
  it("un chip por red, con icono de su tono y su nombre; sin TikTok y con 44 px bajo 1024", async () => {
    mockApi({ [LIST]: page([rosa]) });
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    const names = chips()
      .getAllByRole("button")
      .map((chip) => chip.textContent);
    expect(names).toEqual(["Todas las redes", "WhatsApp", "Instagram", "Messenger", "Pruebas"]);
    expect(chips().getByRole("button", { name: "Todas las redes" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const whatsapp = chips().getByRole("button", { name: "WhatsApp" });
    expect(whatsapp).toHaveAttribute("aria-pressed", "false");
    expect(whatsapp.querySelector("svg")).toHaveClass("text-channel-whatsapp"); // y su nombre
    expect(
      chips().getByRole("button", { name: "Todas las redes" }).querySelector("svg"),
    ).toBeNull();
    expect(chips().getByRole("button", { name: "Pruebas" }).querySelector("svg")).toHaveClass(
      "text-channel-neutral",
    );
    const filters = screen.getByRole("group", { name: "Filtros de conversaciones" });
    for (const chip of within(filters).getAllByRole("button")) {
      expect(chip).toHaveClass("min-h-11", "lg:min-h-9", "shrink-0");
    }
    expect(screen.queryByText(/TikTok/i)).toBeNull();
  });

  it("bajo 1024 px la fila avisa que hay más chips y el enfocado sale del desvanecido", async () => {
    mockApi({ [LIST]: page([rosa]) });
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    const slide = screen.getByRole("group", { name: "Red" }).parentElement!;
    // El desvanecido y el relleno final del mismo ancho, solo bajo 1024 px; el anclaje en cada
    // chip, solo sin movimiento reducido; `relative`: el `sr-only` no ensancha la página.
    expect(slide).toHaveClass("max-lg:edge-fade", "max-lg:pe-8", "max-lg:scroll-pe-8", "relative");
    expect(slide).toHaveClass("overflow-x-auto", "motion-safe:snap-x", "lg:overflow-visible");
    expect(slide).not.toHaveClass("snap-x", "edge-fade");
    for (const chip of within(slide).getAllByRole("button")) expect(chip).toHaveClass("snap-start");
    // Con el foco del teclado dentro, sin anclaje: devolvería el chip bajo el desvanecido.
    expect(slide).toHaveClass("[&:has(:focus-visible)]:snap-none");
    const pruebas = chips().getByRole("button", { name: "Pruebas" });
    const into = vi.fn();
    pruebas.scrollIntoView = into;
    const visible = vi.spyOn(pruebas, "matches");
    visible.mockReturnValue(false); // el foco de un toque o un clic: la fila no se mueve
    act(() => pruebas.focus());
    expect(visible).toHaveBeenCalledWith(":focus-visible");
    expect(into).not.toHaveBeenCalled();
    act(() => pruebas.blur());
    visible.mockReturnValue(true); // el del teclado: sale del desvanecido, sin animación
    act(() => pruebas.focus());
    expect(into).toHaveBeenCalledWith({ block: "nearest", inline: "nearest" });
  });

  it("el chip pide la red a la API, se combina con la bandeja y su número es el de la red", async () => {
    const api = mockApi({
      [LIST]: page([rosa, ines]),
      [COUNT]: ok({ count: 5 }),
      [`${LIST}?platform=INSTAGRAM`]: page([ines]),
      [`${COUNT}?platform=INSTAGRAM`]: ok({ count: 2 }),
      [`${LIST}?box=mine&platform=INSTAGRAM`]: page([]),
      [`${COUNT}?box=mine&platform=INSTAGRAM`]: ok({ count: 0 }),
    });
    show();
    await screen.findByRole("button", { name: "Sin responder, 5 conversaciones sin responder" });
    fireEvent.click(chips().getByRole("button", { name: "Instagram" }));
    expect(await screen.findByRole("button", { name: /Inés Soto/ })).toBeVisible();
    expect(screen.queryByRole("button", { name: /Rosa Díaz/ })).toBeNull();
    expect(chips().getByRole("button", { name: "Instagram" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      await screen.findByRole("button", { name: "Sin responder, 2 conversaciones sin responder" }),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Mías" }));
    expect(
      await screen.findByText("Ninguna conversación de Instagram coincide con los filtros."),
    ).toBeVisible();
    expect(chips().getByRole("button", { name: "Instagram" })).toHaveAttribute(
      "aria-pressed",
      "true",
    ); // cambiar la bandeja conserva la red
    expect(asked(api)).toEqual(
      expect.arrayContaining([
        "/api/v1/o/acme/conversations/?platform=INSTAGRAM",
        "/api/v1/o/acme/conversations/unanswered/?platform=INSTAGRAM",
        "/api/v1/o/acme/conversations/?box=mine&platform=INSTAGRAM",
        "/api/v1/o/acme/conversations/unanswered/?box=mine&platform=INSTAGRAM",
      ]),
    );
    fireEvent.click(chips().getByRole("button", { name: "Todas las redes" }));
    fireEvent.click(screen.getByRole("button", { name: "Todas" }));
    expect(await screen.findByRole("button", { name: /Rosa Díaz/ })).toBeVisible();
    expect(asked(api).filter((url) => url.includes("platform=")).length).toBe(4); // sin la red
  });

  it("«Cargar más» conserva la red y nunca filtra en el navegador lo ya cargado", async () => {
    const api = mockApi({
      [LIST]: page([rosa, ines]),
      [`${LIST}?platform=WHATSAPP`]: page([rosa], "c1"),
      [`${LIST}?platform=WHATSAPP&cursor=c1`]: page([row("3", "Marta Paz", "WHATSAPP")]),
    });
    show();
    await screen.findByRole("button", { name: /Inés Soto/ });
    fireEvent.click(chips().getByRole("button", { name: "WhatsApp" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Inés Soto/ })).toBeNull());
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    expect(await screen.findByRole("button", { name: /Marta Paz/ })).toBeVisible();
    expect(screen.getByRole("button", { name: /Rosa Díaz/ })).toBeVisible();
    expect(asked(api)).toContain("/api/v1/o/acme/conversations/?platform=WHATSAPP&cursor=c1");
    expect(asked(api).some((url) => url.endsWith("?cursor=c1"))).toBe(false);
  });
});

describe("filtro por cuenta del Inbox (UX-INBOX-02d)", () => {
  it("cada cuenta con su red en texto; elegirla filtra la lista y su número en el servidor", async () => {
    const api = mockApi({
      [LIST]: page([rosa, ines]),
      [COUNT]: ok({ count: 5 }),
      [OPTIONS]: page([LIMA], "p2"),
      [`${OPTIONS}&cursor=p2`]: page([IG]),
      [`${LIST}?account=ch-WHATSAPP`]: page([rosa]),
      [`${COUNT}?account=ch-WHATSAPP`]: ok({ count: 1 }),
      [`${LIST}?platform=INSTAGRAM&account=ch-WHATSAPP`]: page([]),
      [`${COUNT}?platform=INSTAGRAM&account=ch-WHATSAPP`]: ok({ count: 0 }),
    });
    show();
    const select = await picker();
    await within(select).findByRole("option", { name: "Instagram · Tienda IG" }); // y la 2.ª página
    const texts = within(select)
      .getAllByRole("option")
      .map((one) => one.textContent);
    expect(texts).toEqual(["Todas las cuentas", "WhatsApp · Ventas Lima", "Instagram · Tienda IG"]);
    expect(select).toHaveClass("h-11");
    fireEvent.change(select, { target: { value: "ch-WHATSAPP" } });
    await waitFor(() => expect(screen.queryByRole("button", { name: /Inés Soto/ })).toBeNull());
    expect(
      await screen.findByRole("button", { name: "Sin responder, 1 conversación sin responder" }),
    ).toBeVisible();
    const icon = document.querySelector("[data-account-icon]"); // la red de la elegida, y en texto
    expect(icon).toHaveClass("text-channel-whatsapp");
    fireEvent.click(chips().getByRole("button", { name: "Instagram" }));
    expect(
      await screen.findByText("Ninguna conversación de Instagram coincide con los filtros."),
    ).toBeVisible();
    expect(asked(api)).toEqual(
      expect.arrayContaining([
        "/api/v1/o/acme/conversations/?account=ch-WHATSAPP",
        "/api/v1/o/acme/conversations/unanswered/?account=ch-WHATSAPP",
        "/api/v1/o/acme/conversations/?platform=INSTAGRAM&account=ch-WHATSAPP",
        "/api/v1/o/acme/conversations/unanswered/?platform=INSTAGRAM&account=ch-WHATSAPP",
      ]),
    );
  });

  it("«Cargar más» conserva la cuenta", async () => {
    const api = mockApi({
      [LIST]: page([rosa, ines]),
      [OPTIONS]: page([LIMA, IG]),
      [`${LIST}?account=ch-WHATSAPP`]: page([rosa], "c1"),
      [`${LIST}?account=ch-WHATSAPP&cursor=c1`]: page([row("3", "Marta Paz", "WHATSAPP")]),
    });
    show();
    fireEvent.change(await picker(), { target: { value: "ch-WHATSAPP" } });
    await waitFor(() => expect(screen.queryByRole("button", { name: /Inés Soto/ })).toBeNull());
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    expect(await screen.findByRole("button", { name: /Marta Paz/ })).toBeVisible();
    expect(asked(api)).toContain("/api/v1/o/acme/conversations/?account=ch-WHATSAPP&cursor=c1");
    expect(asked(api).some((url) => url.endsWith("?cursor=c1"))).toBe(false);
  });

  it("con una sola cuenta, o si sus opciones no cargan, no hay selector", async () => {
    mockApi({ [LIST]: page([rosa]), [OPTIONS]: page([LIMA]) });
    show();
    await screen.findByRole("button", { name: /Rosa Díaz/ });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Estado" })).toBeVisible());
    expect(screen.queryByRole("combobox", { name: "Cuenta" })).toBeNull();
    cleanup();
    mockApi({ [LIST]: page([rosa]) }); // 404: la lista sigue
    show();
    expect(await screen.findByRole("button", { name: /Rosa Díaz/ })).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Cuenta" })).toBeNull();
  });
});
