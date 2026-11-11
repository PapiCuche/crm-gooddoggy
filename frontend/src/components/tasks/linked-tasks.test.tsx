import { cleanup, configure, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

import { ContactCard } from "@/components/contacts/contact-card";
import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Contact, SelfContext, Task } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 15_000 });

const ID = "6f1c2a4e-0b7d-4c8e-9a51-3d2f7e8b9c10";
const TASKS = `/api/v1/o/acme/tasks/?contact_id=${ID}&limit=200`;
const tenant = (...codes: string[]): SelfContext => ({
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: codes.map((code) => ({ code, scopes: [] })),
});
const rosa = {
  ...{ id: ID, display_name: "Rosa Díaz", first_name: "Rosa", last_name: "Díaz" },
  ...{ primary_phone_e164: null, primary_email: null, document_type: null },
  ...{ document_number: null, owner_user_id: null, branch_id: null, source: "WALK_IN" },
  ...{ created_by_user_id: "u1", version: 1, tags: [] },
  ...{ created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-24T20:30:00Z" },
} as Contact;
const task = {
  ...{ id: "t1", type: "CALL", title: "Llamar a Rosa", description: "", assigned_user_id: "u1" },
  ...{ contact: { id: ID, display_name: "Rosa Díaz" }, opportunity_id: "o9", status: "DONE" },
  ...{ due_at: null, is_all_day: false, reminder_at: null, priority: "MEDIUM", version: 3 },
} as Task;
const open = (...codes: string[]) =>
  renderApp(
    <TenantProvider value={tenant(...codes)}>
      <ContactCard contactId={ID} />
    </TenantProvider>,
  );
const tabs = () => within(screen.getByRole("tablist")).getAllByRole("tab");

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ficha del cliente: pestaña «Tareas» (F5-08c)", () => {
  it("solo con tasks.view, y no pide nada hasta abrirla", async () => {
    const api = mockApi({
      [`GET /api/v1/o/acme/contacts/${ID}/`]: { status: 200, body: rosa },
      [`GET ${TASKS}`]: { status: 200, body: { results: [task], next: null } },
    });
    open("contacts.view");
    await screen.findByRole("heading", { level: 1, name: "Rosa Díaz" });
    expect(tabs().map((tab) => tab.textContent)).toEqual(["Resumen", "Notas"]);
    cleanup();
    open("contacts.view", "tasks.view");
    await screen.findByRole("heading", { level: 1, name: "Rosa Díaz" });
    expect(tabs().map((tab) => tab.textContent)).toEqual(["Resumen", "Notas", "Tareas"]);
    expect(api.mock.calls.some(([url]) => String(url) === TASKS)).toBe(false);
    fireEvent.click(screen.getByRole("tab", { name: "Tareas" }));
    // Las de ese cliente, también las cerradas y sin fecha; el cliente no se repite en la fila.
    expect(await screen.findByText("Llamar a Rosa")).toBeVisible();
    expect(screen.getByText("Completada")).toBeVisible();
    expect(screen.getByText("Sin fecha")).toBeVisible();
    expect(screen.queryByRole("link", { name: /Cliente:/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Nueva tarea" })).toBeNull();
  });

  it("«Nueva tarea» ya vinculada al cliente y sin fecha de hoy", async () => {
    const api = mockApi({
      [`GET /api/v1/o/acme/contacts/${ID}/`]: { status: 200, body: rosa },
      [`GET ${TASKS}`]: { status: 200, body: { results: [], next: null } },
      "POST /api/v1/o/acme/tasks/": { status: 201, body: { ...task, due_at: null } },
    });
    open("contacts.view", "tasks.view", "tasks.create");
    await screen.findByRole("heading", { level: 1, name: "Rosa Díaz" });
    fireEvent.click(screen.getByRole("tab", { name: "Tareas" }));
    expect(await screen.findByText("Todavía no hay tareas.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Nueva tarea" }));
    expect(await screen.findByLabelText(/^Fecha/)).toHaveValue("");
    fireEvent.change(screen.getByRole("textbox", { name: "Título" }), {
      target: { value: "Llamar a Rosa" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Guardar tarea" }));
    expect(await screen.findByText(/Tarea creada sin fecha/)).toBeVisible();
    const post = api.mock.calls.find(([, init]) => init?.method === "POST")!;
    expect(JSON.parse(String(post[1]!.body))).toMatchObject({
      ...{ contact_id: ID, title: "Llamar a Rosa", due_at: null, is_all_day: false },
    });
  });
});
