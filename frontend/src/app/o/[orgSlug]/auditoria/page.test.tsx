import { screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import AuditPage from "./page";

const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [],
};

afterEach(() => vi.unstubAllGlobals());

it("`/o/{slug}/auditoria` es la pantalla de auditoría y pide la auditoría", async () => {
  const api = mockApi({
    "GET /api/v1/o/acme/audit/": { status: 200, body: { results: [], next: null } },
  });
  renderApp(
    <TenantProvider value={tenant}>
      <AuditPage />
    </TenantProvider>,
  );
  expect(
    await screen.findByText("Todavía no hay nada en la auditoría de esta organización."),
  ).toBeVisible();
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Auditoría");
  expect(api.mock.calls.map(([url]) => String(url))).toEqual(["/api/v1/o/acme/audit/"]);
});
