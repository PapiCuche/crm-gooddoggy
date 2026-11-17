import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { ManagedQuickReplyCategory, QuickReply, SelfContext } from "@/lib/api/model";
import { renderApp } from "@/test-utils";

import { QuickReplyCatalog } from "./quick-reply-catalog";

const BASE = "/api/v1/o/acme";
const tenant: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  mfa: { required: false, missing: false },
  roles: [],
  permissions: [{ code: "quick_replies.manage", scopes: [] }],
};
const stamp = {
  version: 1,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
};
const cat = (id: string, name: string, sort_order: number, is_active = true) =>
  ({ id, name, sort_order, is_active, ...stamp }) as ManagedQuickReplyCategory;
const qr = (id: string, shortcut: string, category_id: string, is_active = true) =>
  ({
    id,
    shortcut,
    title: `Título ${shortcut}`,
    category_id,
    body: "Hola",
    is_active,
    ...stamp,
    usage_count: 2,
  }) as QuickReply;

type Answer = { status: number; body?: unknown };
// Una API de mentira con estado. `answerOnce`: lo que responde la próxima escritura.
function server() {
  const cats = [cat("c2", "Precios", 2), cat("c1", "Delivery", 1), cat("c3", "Viejas", 9, false)];
  let replies = [qr("r1", "envio", "c1"), qr("r2", "lista", "c2", false)];
  let next: Answer | null = null;
  const json = ({ status, body }: Answer) =>
    new Response(body === undefined ? null : JSON.stringify(body), { status });
  const api = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    const method = init?.method ?? "GET";
    const path = url.pathname.slice(BASE.length);
    if (method === "GET" && path === "/quick-reply-categories/")
      return json({ status: 200, body: { results: cats, next: null } });
    if (method === "GET" && path === "/quick-replies/")
      return json({ status: 200, body: { results: replies, next: null } });
    if (next) {
      const answer = next;
      next = null;
      return json(answer);
    }
    const data = init?.body ? JSON.parse(String(init.body)) : {};
    if (method === "POST" && path === "/quick-replies/") {
      const made = { ...qr(`r${replies.length + 1}`, data.shortcut, data.category_id), ...data };
      replies = [...replies, made];
      return json({ status: 201, body: made });
    }
    const id = path.split("/")[2];
    if (method === "DELETE") {
      replies = replies.filter((one) => one.id !== id);
      return json({ status: 204 });
    }
    const row = replies.find((one) => one.id === id)!;
    const saved = { ...row, ...data, version: row.version + 1 };
    replies = replies.map((one) => (one.id === id ? saved : one));
    return json({ status: 200, body: saved });
  });
  vi.stubGlobal("fetch", api);
  return Object.assign(api, {
    answerOnce: (answer: Answer) => (next = answer),
    // Otra persona la cambia: la lista lo trae cuando se vuelva a pedir.
    retitle: (id: string, title: string) =>
      (replies = replies.map((one) => (one.id === id ? { ...one, title } : one))),
    writes: () =>
      api.mock.calls.flatMap(([url, init]) =>
        init?.method && init.method !== "GET"
          ? [`${init.method} ${String(url).slice(BASE.length)} ${init.body ?? ""}`.trim()]
          : [],
      ),
  });
}
const show = () =>
  renderApp(
    <TenantProvider value={tenant}>
      <QuickReplyCatalog />
    </TenantProvider>,
  );
const form = () => screen.getByRole("form");
const fill = (label: string, value: string) =>
  fireEvent.change(within(form()).getByLabelText(label), { target: { value } });
const submit = (name: string) => fireEvent.click(within(form()).getByRole("button", { name }));

beforeEach(() => {
  document.cookie = "csrftoken=token-de-prueba";
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("pantalla de respuestas rápidas (F6-08c)", () => {
  it("agrupa por categoría en su orden, con las inactivas", async () => {
    server();
    show();
    const groups = await screen.findAllByRole("heading", { level: 2 });
    expect(groups.map((one) => one.textContent)).toEqual([
      "Delivery",
      "Precios",
      "ViejasDesactivada",
    ]);
    const prices = screen.getByRole("list", { name: "Precios" });
    expect(within(prices).getByText("/lista")).toBeInTheDocument();
    expect(within(prices).getByText("Desactivada")).toBeInTheDocument();
    expect(screen.getByText("Sin respuestas en esta categoría.")).toBeInTheDocument();
  });

  it("un 403 se explica", async () => {
    vi.stubGlobal(
      "fetch",
      async () => new Response('{"code":"PERMISSION_DENIED"}', { status: 403 }),
    );
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("No tienes permiso");
  });

  it("crea `/delivery` con vista previa de un cliente de ejemplo, sin la `/` y en minúsculas", async () => {
    const api = server();
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Nueva respuesta" }));
    submit("Crear respuesta"); // vacío: no se envía
    expect(within(form()).getByText("Escribe el atajo.")).toBeInTheDocument();
    fill("Atajo", "/Delivery");
    fill("Título", "Envío a domicilio");
    fill("Categoría", "c2");
    fill("Texto", "Hola {{contact.first_name}}, {{org.name}} te lo lleva hoy. {{x}}");
    expect(within(form()).getByText("Hola Rosa, Acme SAC te lo lleva hoy. {{x}}")).toBeVisible();
    api.answerOnce({ status: 400, body: { code: "VALIDATION_ERROR", fields: { body: ["x"] } } });
    submit("Crear respuesta");
    expect(await within(form()).findByText(/solo admite las variables/)).toBeInTheDocument();
    fill("Texto", "Hola {{contact.first_name}}");
    submit("Crear respuesta");
    expect(await screen.findByText("Respuesta /delivery creada.")).toHaveAttribute(
      "role",
      "status",
    );
    expect(api.writes().at(-1)).toBe(
      'POST /quick-replies/ {"shortcut":"delivery","title":"Envío a domicilio","category_id":"c2","body":"Hola {{contact.first_name}}"}',
    );
    expect(
      within(screen.getByRole("list", { name: "Precios" })).getByText("/delivery"),
    ).toBeVisible();
  });

  it("editar manda solo lo cambiado con su versión; SHORTCUT_TAKEN va al atajo y STALE_VERSION recarga", async () => {
    const api = server();
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Editar /envio" }));
    fill("Atajo", "lista");
    api.answerOnce({ status: 409, body: { code: "SHORTCUT_TAKEN" } });
    submit("Guardar");
    expect(await within(form()).findByText(/Ya hay una respuesta activa/)).toBeInTheDocument();
    fill("Atajo", "envio-lima");
    api.answerOnce({ status: 409, body: { code: "STALE_VERSION" } });
    api.retitle("r1", "Cambiado por otra persona");
    submit("Guardar");
    expect(await screen.findByText(/«\/envio» cambió o ya no existe/)).toHaveAttribute(
      "role",
      "alert",
    );
    expect(screen.queryByRole("form")).toBeNull();
    expect(await screen.findByText("Cambiado por otra persona")).toBeVisible(); // recargada
    expect(api.writes()).toEqual([
      'PATCH /quick-replies/r1/ {"shortcut":"lista","version":1}',
      'PATCH /quick-replies/r1/ {"shortcut":"envio-lima","version":1}',
    ]);
  });

  it("desactiva, reactiva y borra tras confirmar; el foco, al título", async () => {
    const api = server();
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Desactivar /envio" }));
    expect(await screen.findByText("«/envio» desactivada.")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Reactivar /lista" }));
    expect(await screen.findByText("«/lista» reactivada.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Borrar /lista" }));
    expect(screen.getByText(/¿Borrar «\/lista»?/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancelar" })).toHaveFocus();
    const yes = screen.getByRole("button", { name: "Sí, borrar" });
    yes.focus();
    fireEvent.click(yes);
    expect(await screen.findByText("«/lista» borrada.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus(); // la fila se llevó el foco
    expect(screen.queryByText("/lista")).toBeNull();
    expect(api.writes()).toEqual([
      'PATCH /quick-replies/r1/ {"is_active":false,"version":1}',
      'PATCH /quick-replies/r2/ {"is_active":true,"version":1}',
      "DELETE /quick-replies/r2/",
    ]);
  });

  it("mover una respuesta a otra categoría lleva el foco al título", async () => {
    const api = server();
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Editar /envio" }));
    fill("Categoría", "c2");
    const save = within(form()).getByRole("button", { name: "Guardar" });
    save.focus();
    fireEvent.click(save);
    expect(await screen.findByText("Respuesta /envio guardada.")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Precios" })).getByText("/envio")).toBeVisible();
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
    expect(api.writes()).toEqual(['PATCH /quick-replies/r1/ {"category_id":"c2","version":1}']);
  });
});
