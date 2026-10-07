import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { AuditEntry, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { AuditList } from "./audit-list";

const LIST = "GET /api/v1/o/acme/audit/";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "audit.view", scopes: [] }],
};
const entry = (id: string, extra: Partial<AuditEntry> = {}): AuditEntry => ({
  id: `fila-${id}`,
  occurred_at: "2026-10-07T03:15:04.123456Z",
  actor_type: "USER",
  actor_id: `actor-${id}`,
  actor_label: null,
  action: "branch.updated",
  entity_type: "branch",
  entity_id: `entity-${id}`,
  entity_label: null,
  changes: {},
  metadata: {},
  result: "SUCCESS",
  correlation_id: `corr-${id}`,
  ...extra,
});
const screenOf = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <AuditList />
    </TenantProvider>,
  );
const rows = () => within(screen.getByRole("list", { name: "Auditoría" })).getAllByRole("listitem");
const lines = (row: HTMLElement) =>
  [...row.querySelectorAll("p > span, div > p, time")].map((node) => node.textContent);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks(); // el espía de `console.error`, también si el test falló
});

describe("AuditList", () => {
  it("muestra lo que devuelve la API: acción, entidad, actor, fecha y resultado", async () => {
    const renamed = entry("a1", {
      entity_label: "LIM-01",
      actor_label: "Ana López",
      changes: { legal_name: ["Centro", "Centro de Lima"] },
      metadata: { operator: "ops@plataforma.pe", reason: "motivo-privado" },
    });
    const refused = entry("a2", {
      occurred_at: "2026-01-02T03:04:05Z",
      actor_type: "SYSTEM",
      actor_id: null,
      action: "role.permission_granted",
      entity_type: "role",
      result: "DENIED",
    });
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [renamed, refused], next: null } },
    });
    screenOf();
    expect(screen.getByRole("status")).toHaveTextContent("Cargando la auditoría…");
    expect(await screen.findByRole("list", { name: "Auditoría" })).toBeVisible();
    const [first, second] = rows() as [HTMLElement, HTMLElement];
    expect(lines(first)).toEqual([
      "Sucursal editada",
      "branch.updated", // el código, debajo del nombre
      "Sucursal: LIM-01",
      "Por: una persona (Ana López)",
      expect.stringMatching(/^6 oct\.? 2026, 10:15:04\sp\.\sm\.$/), // en la zona de la aplicación
      "Correcto",
    ]);
    expect(lines(second)).toEqual([
      "Permiso concedido a un rol",
      "role.permission_granted",
      "Rol", // sin etiqueta: solo el tipo
      "Por: el sistema",
      expect.stringMatching(/^1 ene\.? 2026, 10:04:05\sp\.\sm\.$/),
      "Denegado",
    ]);
    expect(within(first).getByText("Correcto")).toHaveClass("text-muted");
    expect(within(second).getByText("Denegado")).toHaveClass("text-danger");
    const time = first.querySelector("time");
    expect(time).toHaveAttribute("datetime", "2026-10-07T03:15:04.123456Z");
    const [name, code, entity, actor, result] = first.querySelectorAll("p > span, div > p");
    for (const line of [name, code, entity, actor, result])
      expect(line).toHaveClass("wrap-anywhere");
    // Ni los cambios, ni el contexto, ni identificadores (OBS-F2-73-1), tampoco en atributos.
    for (const hidden of [
      ...Object.keys({ ...renamed.changes, ...renamed.metadata }), // ni los nombres de los campos
      "Centro",
      "ops@plataforma.pe",
      "motivo-privado",
      "fila-",
      "actor-",
      "entity-",
      "corr-",
    ])
      expect(new XMLSerializer().serializeToString(document.body)).not.toContain(hidden);
    expect(screen.getByRole("status")).toHaveTextContent("2 registros en la lista");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Auditoría");
    expect(screen.getByText(/Lo que se hizo en Acme SAC, de lo más reciente/)).toBeVisible();
    expect(api.mock.calls.map(([url]) => String(url))).toEqual(["/api/v1/o/acme/audit/"]);
  });

  it("una acción o un tipo sin nombre propio se enseñan con su código, una sola vez", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const results = [
      entry("a1", { action: "quote.sent", entity_type: "quote", entity_label: "COT-000123" }),
      entry("a2", { action: "constructor", entity_type: "toString" }), // claves heredadas
      entry("a3", { action: "actions.length", entity_type: "__proto__" }),
      entry("a3b", { action: "role.created.0", entity_type: "role.created" }), // ni en un texto
      entry("a4", { action: "branch", entity_type: "entities.role" }), // ni rutas de mensajes
      entry("a5", { actor_type: "ROBOT" as never, result: "__proto__" as never }), // fuera del enum
      entry("a6", { actor_type: "constructor" as never, result: null as never }), // ni un texto
      entry("a7", { actor_type: 7 as never, result: "toString" as never }),
    ];
    mockApi({ [LIST]: { status: 200, body: { results, next: null } } });
    screenOf();
    await screen.findByRole("list", { name: "Auditoría" });
    expect(rows().map((row) => lines(row).slice(0, 2))).toEqual([
      ["quote.sent", "quote: COT-000123"],
      ["constructor", "toString"],
      ["actions.length", "__proto__"],
      ["role.created.0", "role.created"],
      ["branch", "entities.role"],
      ...Array(3).fill(["Sucursal editada", "branch.updated"]),
    ]);
    const unknown = rows().slice(-3);
    expect(unknown.map((row) => lines(row).slice(3))).toEqual([
      ["Por: ROBOT", expect.any(String), "__proto__"],
      ["Por: constructor", expect.any(String), "null"],
      ["Por: 7", expect.any(String), "toString"],
    ]);
    expect(screen.getByText("null")).toHaveClass("text-danger"); // no es «Correcto»: resaltado
    expect(error).not.toHaveBeenCalled(); // ningún mensaje que falte
  });

  it.each([
    ["USER", "Por: una persona"],
    ["AI_AGENT", "Por: un agente de IA"],
    ["SYSTEM", "Por: el sistema"],
    ["INTEGRATION", "Por: una integración"],
    ["PLATFORM_STAFF", "Por: personal de la plataforma"],
  ] as const)("el actor %s se nombra por su tipo", async (actor_type, text) => {
    const results = [entry("a1", { actor_type, result: "FAILED" })];
    mockApi({ [LIST]: { status: 200, body: { results, next: null } } });
    screenOf();
    await screen.findByRole("list", { name: "Auditoría" });
    expect(lines(rows()[0] as HTMLElement).slice(3)).toEqual([text, expect.any(String), "Falló"]);
    expect(screen.getByText("Falló")).toHaveClass("text-danger"); // resaltado, como «Denegado»
    expect(screen.getByRole("status")).toHaveTextContent("1 registro en la lista");
  });

  it("sin filas lo dice, y sin permiso explica el 403", async () => {
    mockApi({ [LIST]: { status: 200, body: { results: [], next: null } } });
    const view = screenOf();
    expect(
      await screen.findByText("Todavía no hay nada en la auditoría de esta organización."),
    ).toBeVisible();
    expect(screen.queryByRole("list", { name: "Auditoría" })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Sin registros en la lista");
    view.unmount();
    mockApi({ [LIST]: { status: 403, body: { code: "PERMISSION_DENIED" } } });
    screenOf();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No tienes permiso para ver la auditoría de esta organización.",
    );
    expect(screen.queryByRole("button", { name: "Reintentar" })).not.toBeInTheDocument();
  });

  it("carga la página siguiente con el cursor", async () => {
    const api = mockApi({
      [LIST]: { status: 200, body: { results: [entry("a1")], next: "abc+/=" } },
      [`${LIST}?cursor=abc%2B%2F%3D`]: {
        status: 200,
        body: {
          results: [entry("a2", { action: "team.created", entity_type: "team" })],
          next: null,
        },
      },
    });
    screenOf();
    fireEvent.click(await screen.findByRole("button", { name: "Cargar más" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("2 registros"));
    expect(rows().map((row) => lines(row)[0])).toEqual(["Sucursal editada", "Equipo creado"]);
    expect(api).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "Cargar más" })).not.toBeInTheDocument();
  });
});
