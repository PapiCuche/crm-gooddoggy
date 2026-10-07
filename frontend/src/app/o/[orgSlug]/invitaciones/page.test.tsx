import { screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import InvitationsPage from "./page";

const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [],
};

afterEach(() => vi.unstubAllGlobals());

it("`/o/{slug}/invitaciones` es la pantalla de invitaciones y pide las invitaciones", async () => {
  const api = mockApi({
    "GET /api/v1/o/acme/invitations/": { status: 200, body: { results: [], next: null } },
  });
  renderApp(
    <TenantProvider value={tenant}>
      <InvitationsPage />
    </TenantProvider>,
  );
  expect(
    await screen.findByText("Esta organización todavía no ha invitado a nadie."),
  ).toBeVisible();
  expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Invitaciones");
  expect(api.mock.calls.map(([url]) => String(url))).toEqual(["/api/v1/o/acme/invitations/"]);
});
