import { act, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { Contact, SelfContext } from "@/lib/api/model";
import { mockApi, renderApp } from "@/test-utils";

import { ContactsList } from "./contacts-list";

// `useSearchParams` de Next solo existe dentro del router: aquí lee la URL real de jsdom y se
// entera de `history.replaceState`, que es lo que hace Next con el suyo.
const bus = vi.hoisted(() => ({ listeners: new Set<() => void>() }));
vi.mock("next/navigation", async () => {
  const { useSyncExternalStore } = await import("react");
  const subscribe = (listener: () => void) => {
    bus.listeners.add(listener);
    return () => void bus.listeners.delete(listener);
  };
  return {
    useSearchParams: () =>
      new URLSearchParams(useSyncExternalStore(subscribe, () => window.location.search)),
  };
});

const PATH = "/o/acme/clientes";
const LIST = "/api/v1/o/acme/contacts/";
const TAGS = "GET /api/v1/o/acme/tags/?limit=200";
const page = (results: unknown[], next: string | null = null) => ({
  status: 200,
  body: { results, next },
});
const tag = { id: "t1", name: "VIP", color: "#ffdb5b", is_active: true, applies_to: "CONTACT" };
const self: SelfContext = {
  user: { id: "u1", email: "ana@acme.pe", first_name: "Ana", last_name: "López" },
  organization: { id: "o1", slug: "acme", name: "Acme SAC" },
  membership_id: "m1",
  roles: [],
  permissions: [{ code: "contacts.view", scopes: [] }],
};
const contact = (id: string, display_name: string, phone: string | null = null): Contact => ({
  ...{ id, display_name, first_name: display_name, last_name: "", primary_phone_e164: phone },
  ...{ primary_email: null, document_type: null, document_number: null, owner_user_id: "u1" },
  ...{ branch_id: null, source: "MANUAL", created_by_user_id: "u1", tags: [] },
  ...{ created_at: "2026-10-23T10:00:00Z", updated_at: "2026-10-23T10:00:00Z", version: 1 },
});
const rosa = contact("c1", "Rosa Díaz", "+51987654321");
const ana = contact("c2", "Ana Luna");
const anaSol = contact("c3", "Ana Sol");

const api = (routes: Record<string, ReturnType<typeof page>>) =>
  mockApi({ [TAGS]: page([tag]), ...routes });
const get = (path: string) => `GET ${LIST}${path}`;
// Las peticiones de la lista (no el catálogo de etiquetas), como se pidieron.
const asked = (fetchMock: ReturnType<typeof api>) =>
  fetchMock.mock.calls.map(([url]) => String(url)).filter((url) => url.startsWith(LIST));
// Cualquier petición responde con una lista (vacía el catálogo de etiquetas): para los textos
// que no se pueden nombrar en una ruta.
const answerAll = () => {
  const fetchMock = mockApi({});
  fetchMock.mockImplementation(async (input) =>
    String(input).includes("/tags/")
      ? new Response(JSON.stringify(page([]).body))
      : new Response(JSON.stringify({ results: [ana], next: null })),
  );
  return fetchMock;
};
const queryOf = (url: string) => new URL(url, "http://x").searchParams;

const show = (url = PATH) => {
  window.history.replaceState(null, "", url);
  return renderApp(
    <TenantProvider value={self}>
      <ContactsList />
    </TenantProvider>,
  );
};
const box = () => screen.getByRole("searchbox", { name: "Buscar cliente" });
const type = (text: string) => fireEvent.change(box(), { target: { value: text } });
const wait = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
const rows = () => within(screen.getByRole("list", { name: "Clientes" })).getAllByRole("listitem");

// El tiempo es del test, pero corre solo mientras Testing Library espera una respuesta.
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  document.cookie = "csrftoken=token-de-prueba";
  const replace = window.history.replaceState.bind(window.history);
  vi.spyOn(window.history, "replaceState").mockImplementation((...args) => {
    replace(...args);
    bus.listeners.forEach((listener) => listener());
  });
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("búsqueda de clientes (F4-13)", () => {
  it("es una caja `type=search` etiquetada, en un buscador, con botones de 44 px", async () => {
    api({ [get("")]: page([rosa]) });
    show();
    await screen.findByText("Rosa Díaz");
    expect(box()).toHaveAttribute("type", "search");
    expect(box()).toHaveClass("h-11", "text-[16px]"); // 44 px; sin ampliación en iOS
    expect(box()).toHaveAttribute("maxlength", "254"); // el máximo de `q` en la API
    expect(box()).toHaveAccessibleDescription(/nombre, el correo o el teléfono/);
    expect(screen.getByRole("search", { name: "Buscar y filtrar clientes" })).toContainElement(
      box(),
    );
    expect(screen.queryByRole("button", { name: "Borrar la búsqueda" })).toBeNull(); // sin texto
    type("ro");
    expect(screen.getByRole("button", { name: "Borrar la búsqueda" })).toHaveClass("size-11");
  });

  it("espera unos 300 ms tras la última tecla y no pregunta en cada una", async () => {
    const fetchMock = api({ [get("")]: page([rosa, ana]), [get("?q=ana")]: page([ana]) });
    show();
    await screen.findByText("Rosa Díaz");
    type("an");
    await wait(200);
    type("ana");
    await wait(250);
    expect(asked(fetchMock)).toEqual([LIST]); // todavía nada
    await wait(60); // el tiempo corre también mientras se espera: hay margen de unos ms
    expect(await screen.findByText("Ana Luna")).toBeVisible();
    expect(asked(fetchMock)).toEqual([LIST, `${LIST}?q=ana`]); // una sola, con el texto entero
    expect(screen.queryByText("Rosa Díaz")).toBeNull();
    expect(screen.getByText("1 cliente en la lista")).toBeInTheDocument(); // el aviso de la lista
  });

  it("pide desde 2 caracteres, o desde 1 si empieza por «+» o es una cifra", async () => {
    const fetchMock = api({
      [get("")]: page([rosa, ana]),
      [get("?q=5")]: page([rosa]),
      [get("?q=%2B5")]: page([rosa]),
      [get("?q=an")]: page([ana]),
    });
    show();
    await screen.findByText("Rosa Díaz");
    type("a");
    await wait(400);
    expect(asked(fetchMock)).toEqual([LIST]); // una letra: no se pide
    expect(box()).toHaveAccessibleDescription("Escribe al menos 2 letras para buscar.");
    expect(rows()).toHaveLength(2); // y la lista sigue entera
    type("5");
    await wait(300);
    await screen.findByText("1 cliente en la lista");
    type("+5");
    await wait(300);
    type("an");
    await wait(300);
    expect(await screen.findByText("Ana Luna")).toBeVisible();
    expect(asked(fetchMock)).toEqual([LIST, `${LIST}?q=5`, `${LIST}?q=%2B5`, `${LIST}?q=an`]);
    expect(box()).toHaveAccessibleDescription(/nombre, el correo o el teléfono/);
  });

  it("un teléfono con +51 encuentra al cliente; el texto se manda sin espacios en los extremos", async () => {
    const fetchMock = api({ [get("")]: page([rosa, ana]), [get("?q=%2B51987")]: page([rosa]) });
    show();
    await screen.findByText("Ana Luna");
    type("  +51987 ");
    await wait(300);
    expect(await screen.findByText("+51987654321")).toBeVisible();
    expect(rows()).toHaveLength(1);
    expect(asked(fetchMock).at(-1)).toBe(`${LIST}?q=%2B51987`);
    expect(box()).toHaveValue("  +51987 "); // lo escrito no se toca mientras se escribe
    expect(window.location.search).toBe("?q=%2B51987"); // la URL, sin espacios (ni «++51987+»)
  });

  it("se combina con la etiqueta y las dos viajan con «Cargar más»", async () => {
    const fetchMock = api({
      [get("")]: page([rosa, ana]),
      [get("?q=ana")]: page([ana], "p2"),
      [get("?q=ana&tag=t1")]: page([ana], "p2"),
      [get("?q=ana&tag=t1&cursor=p2")]: page([anaSol]),
      [get("?tag=t1")]: page([rosa]),
    });
    show();
    await screen.findByText("Rosa Díaz");
    type("ana");
    await wait(300);
    await screen.findByText("1 cliente en la lista");
    fireEvent.change(await screen.findByRole("combobox", { name: "Etiqueta" }), {
      target: { value: "t1" },
    });
    await screen.findByRole("button", { name: "Cargar más" });
    fireEvent.click(screen.getByRole("button", { name: "Cargar más" }));
    expect(await screen.findByText("Ana Sol")).toBeVisible();
    expect(rows()).toHaveLength(2);
    expect(asked(fetchMock)).toEqual([
      ...[LIST, `${LIST}?q=ana`, `${LIST}?q=ana&tag=t1`, `${LIST}?q=ana&tag=t1&cursor=p2`],
    ]);
    // Quitar el texto deja solo la etiqueta: cada combinación es su propia lista.
    fireEvent.click(screen.getByRole("button", { name: "Borrar la búsqueda" }));
    expect(await screen.findByText("Rosa Díaz")).toBeVisible();
    expect(asked(fetchMock).at(-1)).toBe(`${LIST}?tag=t1`);
    expect(screen.queryByText("Ana Sol")).toBeNull();
  });

  it("borrar (botón o Escape) vuelve a la lista entera sin esperar y deja el foco en la caja", async () => {
    const fetchMock = api({
      [get("")]: page([rosa, ana]),
      [get("?q=ana")]: page([ana]),
      [get("?q=ro")]: page([rosa]),
    });
    show();
    await screen.findByText("Rosa Díaz");
    type("ana");
    await wait(300);
    await screen.findByText("1 cliente en la lista");
    // Como una pulsación real: el foco está en el botón, que desaparece al borrar.
    const button = screen.getByRole("button", { name: "Borrar la búsqueda" });
    button.focus();
    expect(box()).not.toHaveFocus();
    fireEvent.click(button);
    expect(box()).toHaveValue("");
    expect(box()).toHaveFocus();
    expect(window.location.search).toBe("");
    expect(await screen.findByText("Rosa Díaz")).toBeVisible();
    expect(rows()).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Borrar la búsqueda" })).toBeNull();
    type("ro");
    await wait(300);
    await screen.findByText("1 cliente en la lista");
    fireEvent.keyDown(box(), { key: "Escape" });
    expect(box()).toHaveValue("");
    expect(box()).toHaveFocus();
    expect(await screen.findByText("2 clientes en la lista")).toBeVisible();
    // Cada vuelta a la lista entera la pide otra vez: la lista no se guarda al cambiar de clave.
    expect(asked(fetchMock)).toEqual([LIST, `${LIST}?q=ana`, LIST, `${LIST}?q=ro`, LIST]);
  });

  it("sin resultados lo dice distinto del vacío de la organización, y también con etiqueta", async () => {
    api({
      [get("")]: page([]),
      [get("?q=zzz")]: page([]),
      [get("?q=zzz&tag=t1")]: page([]),
    });
    show();
    expect(await screen.findByText("Todavía no tienes clientes en la lista.")).toBeVisible();
    type("zzz");
    await wait(300);
    const none = await screen.findByText("Ningún cliente que puedas ver coincide con «zzz».");
    expect(none).toBeVisible();
    expect(screen.queryByText("Todavía no tienes clientes en la lista.")).toBeNull();
    expect(box()).toBeVisible(); // la caja sigue para cambiar el texto
    fireEvent.change(await screen.findByRole("combobox", { name: "Etiqueta" }), {
      target: { value: "t1" },
    });
    expect(
      await screen.findByText(
        "Ningún cliente que puedas ver lleva la etiqueta «VIP» y coincide con «zzz».",
      ),
    ).toBeVisible();
  });

  it("una respuesta vieja no pisa a la nueva", async () => {
    const answers = new Map<string, (reply: Response) => void>();
    const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/tags/")) return Promise.resolve(json(page([]).body));
        return new Promise<Response>((resolve) => answers.set(url, resolve));
      }),
    );
    show();
    await act(() => vi.advanceTimersByTimeAsync(10));
    answers.get(LIST)!(json({ results: [rosa], next: null }));
    await screen.findByText("Rosa Díaz");
    type("ana");
    await wait(300);
    type("ana l");
    await wait(300);
    // La segunda llega primero; la primera, tarde.
    await act(async () => answers.get(`${LIST}?q=ana+l`)!(json({ results: [ana], next: null })));
    expect(await screen.findByText("Ana Luna")).toBeVisible();
    await act(async () =>
      answers.get(`${LIST}?q=ana`)!(json({ results: [rosa, anaSol], next: null })),
    );
    await wait(50);
    expect(rows()).toHaveLength(1);
    expect(rows()[0]).toHaveTextContent("Ana Luna");
    expect(screen.queryByText("Ana Sol")).toBeNull();
  });

  it("el texto va en la URL (`?q=`) con replace, y una URL con `?q=` abre la búsqueda hecha", async () => {
    const fetchMock = api({
      [get("?q=ana")]: page([ana]),
      [get("?q=ana+l")]: page([ana]),
      [get("")]: page([rosa, ana]),
    });
    const push = vi.spyOn(window.history, "pushState");
    show(`${PATH}?q=ana`);
    expect(box()).toHaveValue("ana"); // recargar o compartir: ya está escrito
    expect(await screen.findByText("Ana Luna")).toBeVisible();
    expect(asked(fetchMock)).toEqual([`${LIST}?q=ana`]);
    type("ana l");
    expect(window.location.search).toBe("?q=ana"); // aún no: es un borrador
    await wait(300);
    expect(window.location.pathname + window.location.search).toBe(`${PATH}?q=ana+l`);
    expect(push).not.toHaveBeenCalled(); // replace: «atrás» no recorre cada texto
    // La URL manda: si cambia por fuera (el menú «Clientes»), la caja la sigue.
    await act(async () => {
      window.history.replaceState(null, "", PATH);
    });
    expect(box()).toHaveValue("");
    expect(await screen.findByText("Rosa Díaz")).toBeVisible();
  });

  it("acentos, espacios, «%» y llaves viajan tal cual; el texto nunca es marcado", async () => {
    const odd = 'José  Ñ%50 {name} <img src=x onerror="alert(1)">';
    const fetchMock = api({ [get("")]: page([rosa]) });
    show();
    await screen.findByText("Rosa Díaz");
    // Cualquier texto con `q` responde vacío.
    fetchMock.mockImplementation(async (input) =>
      String(input).includes("/tags/")
        ? new Response(JSON.stringify(page([]).body))
        : new Response(JSON.stringify({ results: [], next: null })),
    );
    type(odd);
    await wait(300);
    const none = await screen.findByText(
      `Ningún cliente que puedas ver coincide con «${odd}».`,
      { normalizer: (text) => text }, // sin juntar los dos espacios
    );
    expect(none.querySelector("img")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
    expect(queryOf(asked(fetchMock).at(-1)!).get("q")).toBe(odd); // exacto, sin escapar
    expect(queryOf(window.location.search).get("q")).toBe(odd);
    expect(box()).toHaveValue(odd);
  });

  it("durante una composición (IME) no se envía a medias; al terminarla, sí", async () => {
    const fetchMock = api({ [get("")]: page([rosa]), [get("?q=%E3%81%AB%E3%81%BB")]: page([ana]) });
    show();
    await screen.findByText("Rosa Díaz");
    fireEvent.compositionStart(box());
    type("に");
    await wait(300);
    type("にほ");
    await wait(2000);
    expect(asked(fetchMock)).toEqual([LIST]); // componiendo: nada, por mucho que pase
    expect(window.location.search).toBe("");
    fireEvent.compositionEnd(box());
    await wait(250);
    expect(asked(fetchMock)).toEqual([LIST]);
    await wait(60);
    expect(await screen.findByText("Ana Luna")).toBeVisible();
    expect(asked(fetchMock)).toHaveLength(2);
  });

  it("al salir de la caja, lo pendiente pasa a la URL sin esperar (no pisa una navegación)", async () => {
    api({ [get("")]: page([rosa]), [get("?q=ana")]: page([ana]) });
    show();
    await screen.findByText("Rosa Díaz");
    vi.mocked(window.history.replaceState).mockClear(); // la de `show` no cuenta
    type("ana");
    expect(window.location.search).toBe("");
    fireEvent.blur(box()); // pulsar un enlace desenfoca antes del clic
    expect(window.location.search).toBe("?q=ana");
    expect(await screen.findByText("Ana Luna")).toBeVisible();
    fireEvent.blur(box()); // sin nada pendiente, no vuelve a escribir
    expect(window.history.replaceState).toHaveBeenCalledTimes(1);
  });

  it("salir en mitad de una composición no escribe a medias, pero la desbloquea", async () => {
    api({ [get("")]: page([rosa]), [get("?q=ni")]: page([ana]) });
    show();
    await screen.findByText("Rosa Díaz");
    fireEvent.compositionStart(box());
    type("ni");
    fireEvent.blur(box());
    expect(window.location.search).toBe(""); // a medias: nada
    await wait(350); // sin `compositionend`, la espera sigue su curso
    expect(window.location.search).toBe("?q=ni");
    expect(await screen.findByText("Ana Luna")).toBeVisible();
  });

  it("Escape en mitad de una composición no borra la caja", async () => {
    api({ [get("")]: page([rosa]) });
    show();
    await screen.findByText("Rosa Díaz");
    fireEvent.compositionStart(box());
    type("にほ");
    fireEvent.keyDown(box(), { key: "Escape", isComposing: true });
    expect(box()).toHaveValue("にほ");
    expect(window.location.search).toBe("");
  });

  it("un `?q=` de la URL con más de 254 caracteres, de control o invisibles se limpia al pedirlo", async () => {
    const long = "é".repeat(300);
    const fetchMock = answerAll();
    show(`${PATH}?q=${encodeURIComponent(long)}`);
    await screen.findByText("Ana Luna");
    expect(Array.from(queryOf(asked(fetchMock)[0]!).get("q")!)).toHaveLength(254);
    expect(box()).toHaveValue(long); // la caja enseña lo que había
    fetchMock.mockClear();
    const dirty = "an\u0000a\u200b\u202e n\n";
    act(() => window.history.replaceState(null, "", `${PATH}?q=${encodeURIComponent(dirty)}`));
    await screen.findByText("Ana Luna");
    expect(queryOf(asked(fetchMock).at(-1)!).get("q")).toBe("ana n");
    fetchMock.mockClear();
    act(() => window.history.replaceState(null, "", `${PATH}?q=%E2%80%8B%E2%80%8B`));
    await wait(50);
    expect(asked(fetchMock).at(-1)).toBe(LIST); // solo invisibles: no es una búsqueda
  });

  it("un «+» solo se pide; un carácter fuera del plano básico cuenta como uno", async () => {
    const fetchMock = answerAll();
    show();
    await screen.findByText("Ana Luna");
    type("😀");
    await wait(400);
    expect(asked(fetchMock)).toEqual([LIST]); // un emoji es un carácter: falta uno
    expect(box()).toHaveAccessibleDescription("Escribe al menos 2 letras para buscar.");
    type("😀😀");
    await wait(300);
    await screen.findByText("Ana Luna");
    expect(queryOf(asked(fetchMock).at(-1)!).get("q")).toBe("😀😀");
    type("+");
    await wait(300);
    await screen.findByText("Ana Luna");
    expect(asked(fetchMock).at(-1)).toBe(`${LIST}?q=%2B`);
  });

  it("un `?q=` con espacios en los extremos se pide recortado", async () => {
    const fetchMock = api({ [get("?q=ana")]: page([ana]) });
    show(`${PATH}?q=%20ana%20`);
    expect(await screen.findByText("Ana Luna")).toBeVisible();
    expect(asked(fetchMock)).toEqual([`${LIST}?q=ana`]);
  });

  it("al escribir en la URL conserva el resto de la dirección: otros parámetros y el ancla", async () => {
    api({ [get("")]: page([rosa]), [get("?q=ana")]: page([ana]) });
    show(`${PATH}?x=1#arriba`);
    await screen.findByText("Rosa Díaz");
    type("ana");
    await wait(300);
    expect(window.location.search).toBe("?x=1&q=ana");
    expect(window.location.hash).toBe("#arriba");
    fireEvent.click(await screen.findByRole("button", { name: "Borrar la búsqueda" }));
    expect(window.location.search).toBe("?x=1");
    expect(window.location.hash).toBe("#arriba");
  });

  it("el foco se queda en la caja mientras se escribe y llega la lista", async () => {
    api({ [get("")]: page([rosa, ana]), [get("?q=ana")]: page([ana]) });
    show();
    await screen.findByText("Rosa Díaz");
    box().focus();
    type("ana");
    await wait(300);
    await screen.findByText("1 cliente en la lista");
    expect(box()).toHaveFocus();
  });
});
