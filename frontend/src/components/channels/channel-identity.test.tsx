import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, configure, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import { Inbox } from "@/components/inbox/inbox";
import { type Conversation, PlatformEnum, type SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { CHANNELS, channelOf, TONES } from "./channel-identity";

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

// CIEDE2000 (Sharma, Wu y Dalal, 2005): distancia perceptiva entre dos colores en CIELAB (D65).
type Lab = [number, number, number];
const lab = (color: Rgb): Lab => {
  const [r, g, b] = color.map(linear) as Rgb;
  const f = (t: number) => (t > 216 / 24389 ? Math.cbrt(t) : ((24389 / 27) * t + 16) / 116);
  const x = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047);
  const y = f(0.2126 * r + 0.7152 * g + 0.0722 * b);
  const z = f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883);
  return [116 * y - 16, 500 * (x - y), 200 * (y - z)];
};
const rad = (deg: number) => (deg * Math.PI) / 180;
const deltaE = ([L1, a1, b1]: Lab, [L2, a2, b2]: Lab) => {
  const g7 = (c: number) => Math.sqrt(c ** 7 / (c ** 7 + 25 ** 7));
  const G = 0.5 * (1 - g7((Math.hypot(a1, b1) + Math.hypot(a2, b2)) / 2));
  const [p1, p2] = [(1 + G) * a1, (1 + G) * a2];
  const [C1, C2] = [Math.hypot(p1, b1), Math.hypot(p2, b2)];
  const hue = (b: number, a: number) => ((Math.atan2(b, a) * 180) / Math.PI + 360) % 360;
  const [h1, h2] = [hue(b1, p1), hue(b2, p2)];
  const zero = C1 * C2 === 0;
  const dh = ((h2 - h1 + 540) % 360) - 180;
  const dH = zero ? 0 : 2 * Math.sqrt(C1 * C2) * Math.sin(rad(dh / 2));
  let H = h1 + h2;
  if (!zero) H = Math.abs(h1 - h2) <= 180 ? H / 2 : H < 360 ? (H + 360) / 2 : (H - 360) / 2;
  const [L, C] = [(L1 + L2) / 2, (C1 + C2) / 2];
  const terms = [
    [-0.17, 1, -30],
    [0.24, 2, 0],
    [0.32, 3, 6],
    [-0.2, 4, -63],
  ] as const;
  const T = terms.reduce((sum, [k, n, d]) => sum + k * Math.cos(rad(n * H + d)), 1);
  const SL = 1 + (0.015 * (L - 50) ** 2) / Math.sqrt(20 + (L - 50) ** 2);
  const [SC, SH] = [1 + 0.045 * C, 1 + 0.015 * C * T];
  const RT = -Math.sin(rad(60 * Math.exp(-(((H - 275) / 25) ** 2)))) * 2 * g7(C);
  const [l, c, h] = [(L2 - L1) / SL, (C2 - C1) / SC, dH / SH];
  return Math.sqrt(l * l + c * c + h * h + RT * c * h);
};

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
    for (const tone of Object.keys(TONES)) {
      const value = token(`channel-${tone}`);
      expect(states).not.toContain(value);
      for (const [name, bg] of Object.entries(backgrounds)) {
        expect(ratio(rgb(value), bg), `${tone} sobre ${name}`).toBeGreaterThanOrEqual(3);
      }
    }
    // El verde de marca de WhatsApp no sirve sobre claro: por eso no es el token.
    expect(ratio(rgb("#25d366"), backgrounds.surface)).toBeLessThan(3);
  });

  it("ΔE2000 coincide con los pares de referencia de Sharma et al.", () => {
    const pairs: [Lab, Lab, number][] = [
      [[50, 2.6772, -79.7751], [50, 0, -82.7485], 2.0425],
      [[50, 0, 0], [50, -1, 2], 2.3669],
      [[50, 2.5, 0], [73, 25, -18], 27.1492],
      [[60.2574, -34.0099, 36.2677], [60.4626, -34.1751, 39.4387], 1.2644],
      [[90.9257, -0.5406, -0.9208], [88.6381, -0.8985, -0.7239], 1.5381],
      [[2.0776, 0.0795, -1.135], [0.9033, -0.0636, -0.5514], 0.9082],
    ];
    for (const [one, two, expected] of pairs) {
      expect(deltaE(one, two)).toBeCloseTo(expected, 4);
      expect(deltaE(two, one)).toBeCloseTo(expected, 4);
    }
  });

  it("ningún tono de canal se parece a un color de estado ni a la miel (ΔE2000 ≥ 5)", () => {
    const states = ["success", "danger", "accent"].map((name) => lab(rgb(token(name))));
    for (const tone of Object.keys(TONES)) {
      const value = lab(rgb(token(`channel-${tone}`)));
      for (const state of states) expect(deltaE(value, state), tone).toBeGreaterThanOrEqual(5);
    }
    // Un verde que solo cambia un dígito del de «leído» no pasa.
    expect(deltaE(lab(rgb("#1a7f38")), lab(rgb(token("success"))))).toBeLessThan(5);
  });
});

// Lista y chat dicen la red en texto, con su icono; el punto de la fila solo se suma.
const tenant = {
  ...{ user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" } },
  ...{ organization: { id: "o1", slug: "acme", name: "Acme SAC" }, membership_id: "m1" },
  ...{ mfa: { required: false, missing: false }, roles: [] },
  permissions: [{ code: "conversations.view", scopes: [] }],
} as SelfContext;
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
  ])(
    "la fila %s y su chat dicen «%s» en texto, aparte de la cuenta",
    async (id, platform, label, dot) => {
      // La cuenta repite el nombre de la red («WhatsApp, WhatsApp Tienda»): se lee aparte, no se oculta.
      const rows = ["1", "2", "3", "4", "5"].map((n) => conversation(n, platform, `${label} ${n}`));
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
      expect(row).toHaveTextContent(`CONV-0000${id}${label}·, ${label} ${id}Abierta`);
      expect(row).toHaveAccessibleName(expect.stringContaining(`${label}, ${label} ${id}`));
      expect(within(row).getByText("·")).toHaveAttribute("aria-hidden", "true");
      expect(row.querySelector("[data-channel-dot]")).toHaveClass(dot);
      expect(row.querySelector("[data-channel-dot]")).toHaveAttribute("aria-hidden", "true");
      // El color va en el icono (oculto al lector), nunca en el texto del nombre.
      const mark = within(row).getByText(label);
      expect(mark.className).not.toMatch(/channel/);
      expect(mark.querySelector("svg")).toHaveClass(dot.replace("bg-", "text-"));
      fireEvent.click(row);
      expect(row).toHaveClass("border-l-4", "bg-accent/30"); // la elegida conserva su marca
      const heading = await screen.findByRole("heading", { level: 2, name: `Cliente ${id}` });
      const header = within(heading.closest("header")!);
      expect(header.getByText(label)).toBeVisible();
      expect(header.getByText(`${label} ${id}`)).toHaveClass("text-muted");
      expect(header.getByText(`${label} ${id}`).tagName).toBe("BDI"); // texto ajeno, aislado
    },
  );
});
