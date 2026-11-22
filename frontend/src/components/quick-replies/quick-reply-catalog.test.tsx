import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
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
type Row = { id: string; version: number };
// Una API de mentira con estado. `answerOnce`: lo que responde la próxima escritura.
function server(
  start = [
    { ...cat("c2", "Precios", 2), version: 2 },
    cat("c1", "Delivery", 1),
    cat("c3", "Viejas", 9, false),
  ],
) {
  let cats = start;
  let replies = [qr("r1", "envio", "c1"), qr("r2", "lista", "c2", false)];
  let next: Answer | null = null;
  let broken = 0; // cuántas lecturas de la lista fallan aún
  const json = ({ status, body }: Answer) =>
    new Response(body === undefined ? null : JSON.stringify(body), { status });
  const api = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://x");
    const method = init?.method ?? "GET";
    const path = url.pathname.slice(BASE.length);
    if (method === "GET" && path === "/quick-reply-categories/")
      return json({ status: 200, body: { results: cats, next: null } });
    if (method === "GET" && path === "/quick-replies/") {
      if (broken > 0) {
        broken -= 1;
        return json({ status: 500, body: { code: "INTERNAL_ERROR" } });
      }
      return json({ status: 200, body: { results: replies, next: null } });
    }
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
    if (method === "POST") {
      const made = cat(`c${cats.length + 1}`, data.name, data.sort_order ?? 99);
      cats = [...cats, made];
      return json({ status: 201, body: made });
    }
    const [, kind, id] = path.split("/");
    if (method === "DELETE") {
      replies = replies.filter((one) => one.id !== id);
      return json({ status: 204 });
    }
    const rows: Row[] = kind === "quick-replies" ? replies : cats;
    const row = rows.find((one) => one.id === id)!;
    const saved = { ...row, ...data, version: row.version + 1 };
    if (kind === "quick-replies") replies = replies.map((one) => (one.id === id ? saved : one));
    else cats = cats.map((one) => (one.id === id ? saved : one));
    return json({ status: 200, body: saved });
  });
  vi.stubGlobal("fetch", api);
  return Object.assign(api, {
    answerOnce: (answer: Answer) => (next = answer),
    // Otra persona la cambia: la lista lo trae cuando se vuelva a pedir.
    retitle: (id: string, title: string) =>
      (replies = replies.map((one) => (one.id === id ? { ...one, title } : one))),
    addCategory: (name: string) => (cats = [...cats, cat("cx", name, 50)]),
    setCategory: (id: string, patch: Partial<ManagedQuickReplyCategory>) =>
      (cats = cats.map((one) => (one.id === id ? { ...one, ...patch } : one))),
    drop: (id: string) => (replies = replies.filter((one) => one.id !== id)),
    breakList: (times: number) => (broken = times),
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
  it("agrupa por categoría en su orden, con las inactivas, y lista las categorías", async () => {
    server();
    show();
    const groups = await screen.findAllByRole("heading", { level: 2 });
    expect(groups.map((one) => one.textContent)).toEqual([
      "Delivery",
      "Precios",
      "ViejasDesactivada",
      "Categorías",
    ]);
    const categories = screen.getByRole("list", { name: "Categorías" });
    expect(within(categories).getByText("Orden 9")).toBeInTheDocument();
    expect(document.activeElement).toBe(document.body); // cargar no mueve el foco
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
    fill("Texto", "Hola {{contact.first_name}}, {{org.name}} te lo lleva. {{x}} {{constructor}}");
    const shown = "Hola Rosa, Acme SAC te lo lleva. {{x}} {{constructor}}";
    expect(within(form()).getByText(shown)).toBeVisible();
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

  it("desactiva (una vez aunque se pulse dos), reactiva y borra tras confirmar; el foco, al título", async () => {
    const api = server();
    show();
    const off = await screen.findByRole("button", { name: "Desactivar /envio" });
    off.focus();
    fireEvent.click(off);
    fireEvent.click(off);
    expect(await screen.findByText("«/envio» desactivada.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reactivar /envio" })).toHaveFocus(); // sigue ahí
    fireEvent.click(await screen.findByRole("button", { name: "Reactivar /lista" }));
    expect(await screen.findByText("«/lista» reactivada.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Borrar /lista" }));
    expect(screen.getByText(/¿Borrar «\/lista»?/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancelar" })).toHaveFocus();
    const yes = screen.getByRole("button", { name: "Sí, borrar" });
    expect(yes).toHaveAccessibleDescription(/No se puede deshacer/);
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

  it("sin el foco en la página (Safari), el título no lo toma; un 404 que se lleva la fila, sí", async () => {
    const api = server();
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Desactivar /envio" }));
    expect(await screen.findByText("«/envio» desactivada.")).toBeInTheDocument();
    expect(document.activeElement).toBe(document.body);
    const on = screen.getByRole("button", { name: "Reactivar /envio" });
    on.focus();
    api.answerOnce({ status: 404, body: { code: "NOT_FOUND" } });
    api.drop("r1"); // otra persona la borró
    fireEvent.click(on);
    expect(await screen.findByText(/«\/envio» cambió o ya no existe/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("/envio")).toBeNull());
    expect(screen.getByRole("heading", { level: 1 })).toHaveFocus();
  });

  it("si la recarga falla lo dice, y un refetch posterior no mueve el foco", async () => {
    const api = server();
    const view = show();
    const off = await screen.findByRole("button", { name: "Desactivar /envio" });
    off.focus();
    api.breakList(2); // la lectura y su reintento
    fireEvent.click(off);
    expect(await screen.findByText(/Se guardó, pero la lista no se pudo/)).toBeInTheDocument();
    off.blur();
    await act(() => view.client.invalidateQueries());
    expect(await screen.findByRole("button", { name: "Reactivar /envio" })).toBeVisible();
    expect(document.activeElement).toBe(document.body);
  });

  it("sin categorías activas, remite a «Categorías»", async () => {
    server([cat("c1", "Viejas", 1, false)]);
    show();
    expect(await screen.findByText(/No hay categorías activas. Crea o reactiva una/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Nueva respuesta" })).toBeNull();
  });

  it("crea una categoría con su orden (CATEGORY_NAME_TAKEN recarga) y la usa en una respuesta", async () => {
    const api = server();
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Nueva categoría" }));
    fill("Nombre", "Garantía");
    fill("Orden", "1.5");
    submit("Crear categoría");
    expect(within(form()).getByText(/número entero entre/)).toBeInTheDocument();
    for (const outside of ["32768", "-32769"]) {
      fill("Orden", outside);
      submit("Crear categoría");
      expect(within(form()).getByText(/número entero entre/)).toBeInTheDocument();
    }
    fill("Orden", "");
    api.answerOnce({ status: 409, body: { code: "CATEGORY_NAME_TAKEN" } });
    api.addCategory("Garantía");
    submit("Crear categoría");
    expect(await within(form()).findByText(/Ya hay una categoría con ese nombre/)).toBeVisible();
    expect(await screen.findByRole("heading", { level: 2, name: "Garantía" })).toBeVisible();
    fill("Nombre", "Garantías");
    api.answerOnce({ status: 400, body: { code: "VALIDATION_ERROR", fields: { name: ["x"] } } });
    submit("Crear categoría");
    expect(await within(form()).findByText(/El nombre no es válido/)).toBeVisible();
    fill("Orden", "0");
    api.answerOnce({
      status: 400,
      body: { code: "VALIDATION_ERROR", fields: { sort_order: ["x"] } },
    });
    submit("Crear categoría");
    expect(await within(form()).findByText(/número entero entre/)).toBeVisible();
    submit("Crear categoría");
    expect(await screen.findByText("Categoría «Garantías» creada.")).toBeInTheDocument();
    const groups = screen.getAllByRole("heading", { level: 2 }).map((one) => one.textContent);
    expect(groups.slice(0, 2)).toEqual(["Garantías", "Delivery"]); // orden 0, antes que el 1
    fireEvent.click(screen.getByRole("button", { name: "Nueva respuesta" }));
    fill("Atajo", "garantia");
    fill("Título", "Garantía");
    fill("Categoría", "c5");
    fill("Texto", "Un año de garantía.");
    submit("Crear respuesta");
    const list = await screen.findByRole("list", { name: "Garantías" });
    expect(within(list).getByText("/garantia")).toBeVisible();
    expect(api.writes().slice(0, 5)).toEqual([
      'POST /quick-reply-categories/ {"name":"Garantía"}',
      'POST /quick-reply-categories/ {"name":"Garantías"}',
      'POST /quick-reply-categories/ {"name":"Garantías","sort_order":0}',
      'POST /quick-reply-categories/ {"name":"Garantías","sort_order":0}',
      'POST /quick-replies/ {"shortcut":"garantia","title":"Garantía","category_id":"c5","body":"Un año de garantía."}',
    ]);
  });

  it("renombra y reordena con un solo PATCH; sin cambios no escribe; STALE_VERSION recarga", async () => {
    const api = server();
    show();
    const edit = "Renombrar u ordenar la categoría Delivery";
    fireEvent.click(await screen.findByRole("button", { name: edit }));
    submit("Guardar");
    expect(await screen.findByText("Sin cambios en «Delivery»: no había nada que guardar."));
    fireEvent.click(screen.getByRole("button", { name: edit }));
    fill("Orden", "");
    submit("Guardar");
    expect(within(form()).getByText("Escribe el orden: un número entero.")).toBeVisible();
    fill("Orden", "1");
    fill("Nombre", "Otra");
    api.answerOnce({ status: 409, body: { code: "CATEGORY_NAME_TAKEN" } });
    api.addCategory("Otra");
    submit("Guardar");
    expect(await within(form()).findByText(/Ya hay una categoría con ese nombre/)).toBeVisible();
    expect(await screen.findByRole("heading", { level: 2, name: "Otra" })).toBeVisible();
    fill("Nombre", "Envíos");
    fill("Orden", "5");
    submit("Guardar");
    expect(await screen.findByText("Categoría «Envíos» guardada.")).toBeInTheDocument();
    const groups = screen.getAllByRole("heading", { level: 2 }).map((one) => one.textContent);
    expect(groups.slice(0, 3)).toEqual(["Precios", "Envíos", "ViejasDesactivada"]);
    fireEvent.click(
      screen.getByRole("button", { name: "Renombrar u ordenar la categoría Precios" }),
    );
    fill("Nombre", "Tarifas");
    api.answerOnce({ status: 409, body: { code: "STALE_VERSION" } });
    api.retitle("r2", "Cambiada por otra persona");
    submit("Guardar");
    expect(await screen.findByRole("alert")).toHaveTextContent("«Precios» cambió o ya no existe");
    expect(screen.queryByRole("form")).toBeNull();
    expect(await screen.findByText("Cambiada por otra persona")).toBeVisible(); // recargada
    fireEvent.click(
      screen.getByRole("button", { name: "Renombrar u ordenar la categoría Viejas" }),
    );
    fill("Nombre", "Antiguas");
    api.answerOnce({ status: 404, body: { code: "NOT_FOUND" } });
    submit("Guardar");
    expect(await screen.findByText(/«Viejas» cambió o ya no existe/)).toBeInTheDocument();
    expect(screen.queryByRole("form")).toBeNull();
    expect(api.writes()).toEqual([
      'PATCH /quick-reply-categories/c1/ {"name":"Otra","version":1}',
      'PATCH /quick-reply-categories/c1/ {"name":"Envíos","sort_order":5,"version":1}',
      'PATCH /quick-reply-categories/c2/ {"name":"Tarifas","version":2}',
      'PATCH /quick-reply-categories/c3/ {"name":"Antiguas","version":1}',
    ]);
  });

  it("desactiva y reactiva una categoría; CATEGORY_IN_USE lo explica y recarga", async () => {
    const api = server();
    show();
    api.answerOnce({ status: 409, body: { code: "CATEGORY_IN_USE" } });
    api.retitle("r1", "Cambiada por otra persona");
    const delivery = await screen.findByRole("button", {
      name: "Desactivar la categoría Delivery",
    });
    fireEvent.click(delivery);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La categoría «Delivery» tiene respuestas activas",
    );
    expect(await screen.findByText("Cambiada por otra persona")).toBeVisible(); // recargada
    fireEvent.click(delivery); // tras un error, el botón vuelve a escribir
    expect(await screen.findByText("«Delivery» desactivada.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Desactivar la categoría Precios" }));
    expect(await screen.findByText("«Precios» desactivada.")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Reactivar la categoría Viejas" }));
    expect(await screen.findByText("«Viejas» reactivada.")).toBeInTheDocument();
    expect(api.writes()).toEqual([
      'PATCH /quick-reply-categories/c1/ {"is_active":false,"version":1}',
      'PATCH /quick-reply-categories/c1/ {"is_active":false,"version":1}',
      'PATCH /quick-reply-categories/c2/ {"is_active":false,"version":2}',
      'PATCH /quick-reply-categories/c3/ {"is_active":true,"version":1}',
    ]);
  });

  it("la respuesta ofrece las categorías activas, más la inactiva en la que ya está", async () => {
    const api = server();
    api.setCategory("c1", { is_active: false });
    show();
    const options = () =>
      within(form())
        .getAllByRole("option")
        .map((one) => one.textContent);
    fireEvent.click(await screen.findByRole("button", { name: "Nueva respuesta" }));
    expect(options()).toEqual(["Precios"]);
    fireEvent.click(within(form()).getByRole("button", { name: "Cancelar" }));
    fireEvent.click(screen.getByRole("button", { name: "Editar /envio" }));
    expect(options()).toEqual(["Delivery", "Precios"]);
  });

  it("un STALE cuya recarga trae lo mismo no deja la marca: un refetch posterior no mueve el foco", async () => {
    const api = server();
    const view = show();
    fireEvent.click(await screen.findByRole("button", { name: "Editar /envio" }));
    fill("Título", "Otro");
    api.answerOnce({ status: 409, body: { code: "STALE_VERSION" } });
    submit("Guardar");
    expect(await screen.findByText(/«\/envio» cambió o ya no existe/)).toBeInTheDocument();
    await waitFor(() => expect(view.client.isFetching()).toBe(0)); // la recarga, igual
    (document.activeElement as HTMLElement).blur();
    api.retitle("r1", "Cambiada por otra persona");
    await act(() => view.client.invalidateQueries());
    expect(await screen.findByText("Cambiada por otra persona")).toBeVisible();
    expect(document.activeElement).toBe(document.body);
  });
});
