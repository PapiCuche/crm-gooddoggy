import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, configure, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import { Inbox } from "@/components/inbox/inbox";
import { type Conversation, PlatformEnum, type SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { CHANNELS, ChannelMark, channelOf, TONES } from "./channel-identity";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/o/acme/inbox",
}));
configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });
afterEach(cleanup);

// Los tokens tal como están en `globals.css` (`:root`), con `var(--x)` resuelto.
// Ruta de disco: Vite convierte `new URL("….css", import.meta.url)` en la URL de un recurso.
const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "../../app/globals.css"), "utf8");
const root = css.slice(css.indexOf(":root {"), css.indexOf("}", css.indexOf(":root {")));
const token = (name: string): string => {
  const value = new RegExp(`--${name}:\\s*([^;]+);`).exec(root)?.[1]?.trim();
  if (!value) throw new Error(`falta --${name}`);
  const alias = /^var\(--([\w-]+)\)$/.exec(value)?.[1];
  return alias ? token(alias) : value.toLowerCase();
};
type Rgb = [number, number, number];
const rgb = (hex: string): Rgb => {
  expect(hex).toMatch(/^#[0-9a-f]{6}$/);
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as Rgb;
};
// WCAG 2.x: luminancia relativa y razón de contraste.
const linear = (c: number) => {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
};
const luminance = ([r, g, b]: Rgb) => 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
const ratio = (a: Rgb, b: Rgb) => {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
};
const over = ([r, g, b]: Rgb, alpha: number, [R, G, B]: Rgb): Rgb =>
  [r * alpha + R * (1 - alpha), g * alpha + G * (1 - alpha), b * alpha + B * (1 - alpha)].map(
    Math.round,
  ) as Rgb;

describe("identidad de canal (UX-INBOX-01)", () => {
  it("cada plataforma del contrato tiene su entrada; solo las de Meta son `social`", () => {
    for (const platform of Object.values(PlatformEnum)) {
      expect(Object.hasOwn(CHANNELS, platform)).toBe(true);
      expect(channelOf(platform)).toBe(CHANNELS[platform]);
    }
    const social = Object.entries(CHANNELS).filter(([, one]) => one.social);
    expect(social.map(([key]) => key).sort()).toEqual(["INSTAGRAM", "MESSENGER"]);
  });

  it.each(["TIKTOK", "", "whatsapp", "constructor", "toString", "__proto__", "hasOwnProperty"])(
    "«%s» es «Otra red», neutra y sin `social`",
    (platform) => {
      expect(channelOf(platform)).toBe(CHANNELS.UNKNOWN);
      expect(channelOf(platform)).toMatchObject({ tone: "neutral", social: false });
    },
  );

  it("cada tono de canal tiene ≥3:1 sobre los fondos claros y no es un color de estado", () => {
    const backgrounds = {
      surface: rgb(token("surface")),
      background: rgb(token("background")),
      "surface-raised": rgb(token("surface-raised")),
      // La fila elegida: `bg-accent/30` sobre el lienzo de la página o sobre papel.
      "accent/30 + background": over(rgb(token("accent")), 0.3, rgb(token("background"))),
      "accent/30 + surface": over(rgb(token("accent")), 0.3, rgb(token("surface"))),
    };
    const states = [token("success"), token("danger"), token("accent")];
    const measured: Record<string, string> = {};
    for (const tone of Object.keys(TONES)) {
      const value = token(`channel-${tone}`);
      expect(states).not.toContain(value);
      for (const [name, bg] of Object.entries(backgrounds)) {
        const r = ratio(rgb(value), bg);
        measured[`${tone} / ${name}`] = r.toFixed(2);
        expect(r, `${tone} sobre ${name}`).toBeGreaterThanOrEqual(3);
      }
    }
    expect(Object.keys(measured)).toHaveLength(4 * 5);
    // El verde de marca de WhatsApp no sirve sobre claro: por eso no es el token.
    expect(ratio(rgb("#25d366"), backgrounds.surface)).toBeLessThan(3);
  });

  it.each([
    ["WHATSAPP", "WhatsApp", "text-channel-whatsapp"],
    ["INSTAGRAM", "Instagram", "text-channel-instagram"],
    ["MESSENGER", "Messenger", "text-channel-messenger"],
    ["SANDBOX", "Pruebas", "text-channel-neutral"],
    ["TIKTOK", "Otra red", "text-channel-neutral"],
    ["constructor", "Otra red", "text-channel-neutral"],
  ])("%s se lee «%s»: el color va en el icono, nunca en el texto", (platform, label, tone) => {
    const { container } = renderApp(<ChannelMark platform={platform} />);
    const mark = container.firstElementChild!;
    expect(mark).toHaveTextContent(new RegExp(`^${label}$`));
    expect(mark).toHaveClass("text-foreground");
    expect(mark.className).not.toMatch(/channel/);
    const icon = mark.querySelector("svg")!;
    expect(icon).toHaveAttribute("aria-hidden", "true");
    expect(icon).toHaveClass(tone);
  });
});

// Lista y chat dicen la red en texto, con su icono; el punto de la fila solo se suma.
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: [{ code: "conversations.view", scopes: [] }],
};
const ok = (body: unknown) => ({ status: 200, body });
const conversation = (id: string, platform: string, name: string) =>
  ({
    ...{ id, number: `CONV-0000${id}`, status: "OPEN", attention_mode: "HUMAN" },
    ...{ assignee_kind: "UNASSIGNED", assigned_user_id: null, assigned_team_id: null },
    ...{ contact: { id: `c${id}`, display_name: `Cliente ${id}` } },
    ...{ channel: { id: `ch${id}`, platform, name, text_max: 1000, text_unit: "CHARACTERS" } },
    ...{ last_message_at: "2026-11-04T15:30:00Z" },
    ...{ last_inbound_at: "2026-11-04T15:30:00Z", last_message_preview: "Hola" },
    ...{ last_message_direction: "INBOUND", version: 1 },
  }) as unknown as Conversation;

describe("lista y chat (UX-INBOX-01)", () => {
  it.each([
    ["1", "WHATSAPP", "WhatsApp", "bg-channel-whatsapp"],
    ["2", "INSTAGRAM", "Instagram", "bg-channel-instagram"],
    ["3", "MESSENGER", "Messenger", "bg-channel-messenger"],
    ["4", "SANDBOX", "Pruebas", "bg-channel-neutral"],
    ["5", "TIKTOK", "Otra red", "bg-channel-neutral"],
  ])("la fila %s y su chat dicen «%s» en texto", async (id, platform, label, dot) => {
    const rows = ["1", "2", "3", "4", "5"].map((n) => conversation(n, platform, `Cuenta ${n}`));
    const list = "GET /api/v1/o/acme/conversations/";
    mockApi({
      [list]: ok({ results: rows, next: null }),
      [`${list}unanswered/`]: ok({ count: 0 }),
      [`${list}${id}/`]: ok(rows[Number(id) - 1]),
      [`${list}${id}/messages/`]: ok({ results: [], next: null }),
    });
    renderApp(
      <TenantProvider value={tenant}>
        <Inbox />
      </TenantProvider>,
    );
    const row = await screen.findByRole("button", { name: new RegExp(`Cliente ${id}`) });
    expect(row).toHaveTextContent(`CONV-0000${id}${label}Cuenta ${id}Abierta`);
    expect(row).toHaveAccessibleName(expect.stringContaining(label));
    expect(row.querySelector("[data-channel-dot]")).toHaveClass(dot);
    expect(row.querySelector("[data-channel-dot]")).toHaveAttribute("aria-hidden", "true");
    fireEvent.click(row);
    expect(row).toHaveClass("border-l-4", "bg-accent/30"); // la elegida conserva su marca
    const heading = await screen.findByRole("heading", { level: 2, name: `Cliente ${id}` });
    const header = within(heading.closest("header")!);
    expect(header.getByText(label)).toBeVisible();
  });
});
