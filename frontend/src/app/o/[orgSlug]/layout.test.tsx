import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/o/acme_01",
}));

import { TenantGate } from "@/components/app-shell/tenant-gate";

import TenantLayout from "./layout";

describe("TenantLayout", () => {
  it.each(["caf%C3%A9", "..%2F..%2Fx", "a b", "", "x".repeat(64)])(
    "rechaza el slug %j",
    async (slug) => {
      await expect(
        TenantLayout({ children: null, params: Promise.resolve({ orgSlug: slug }) }),
      ).rejects.toThrow("NEXT_NOT_FOUND");
    },
  );

  it.each(["acme_01", "mi-tienda", "x".repeat(63)])(
    "pone la guardia con el slug %j",
    async (slug) => {
      const page = await TenantLayout({
        children: "pantalla",
        params: Promise.resolve({ orgSlug: slug }),
      });
      expect(page.type).toBe(TenantGate);
      expect(page.props).toEqual({ orgSlug: slug, children: "pantalla" });
    },
  );
});
