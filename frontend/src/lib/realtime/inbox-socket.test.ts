import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  backoff,
  connectInbox,
  type InboxEvent,
  jittered,
  MAX_FRAME,
  parseInboxEvent,
  type SocketLike,
  PING_EVERY,
  STABLE,
} from "./inbox-socket";

// El doble del WebSocket: guarda cada conexión y deja a la prueba abrirla, mandarle texto o cerrarla.
class Fake implements SocketLike {
  static all: Fake[] = [];
  onopen: SocketLike["onopen"] = null;
  onmessage: SocketLike["onmessage"] = null;
  onclose: SocketLike["onclose"] = null;
  onerror: SocketLike["onerror"] = null;
  closed = false;
  sent: string[] = [];
  constructor(readonly url: string) {
    Fake.all.push(this);
  }
  send = (data: string) => {
    this.sent.push(data);
  };
  close = () => {
    this.closed = true;
  };
  open = () => this.onopen?.({});
  say = (data: unknown) => this.onmessage?.({ data });
  drop = (code = 1006) => this.onclose?.({ code });
}
const last = () => Fake.all[Fake.all.length - 1]!;

function start() {
  const events: InboxEvent[] = [];
  const resync = vi.fn();
  const stop = connectInbox({
    url: "ws://x/ws/o/acme/inbox/",
    onEvent: (event) => events.push(event),
    onResync: resync,
    create: (url) => new Fake(url),
    random: () => 1, // sin recorte: las esperas son las de `backoff`
  });
  return { events, resync, stop };
}

beforeEach(() => {
  Fake.all = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe("parseInboxEvent", () => {
  it("acepta los tres tipos y el message_id opcional", () => {
    const frame = (body: object) => JSON.stringify(body);
    expect(parseInboxEvent(frame({ type: "message.received", conversation_id: "c1" }))).toEqual({
      type: "message.received",
      conversation_id: "c1",
    });
    expect(
      parseInboxEvent(
        frame({ type: "message.updated", conversation_id: "c1", message_id: "m-9", extra: 1 }),
      ),
    ).toEqual({ type: "message.updated", conversation_id: "c1", message_id: "m-9" });
    expect(parseInboxEvent(frame({ type: "conversation.updated", conversation_id: "c1" }))).toEqual(
      {
        type: "conversation.updated",
        conversation_id: "c1",
      },
    );
  });

  it("ignora lo que no es lo acordado: binario, JSON roto, tipos nuevos e ids raros", () => {
    const frame = (body: unknown) => JSON.stringify(body);
    for (const bad of [
      new ArrayBuffer(4),
      null,
      "",
      "{roto",
      "null",
      "[]",
      "7",
      frame({ type: "typing", conversation_id: "c1" }),
      frame({ conversation_id: "c1" }),
      frame({ type: "message.received" }),
      frame({ type: "message.received", conversation_id: 5 }),
      frame({ type: "message.received", conversation_id: "../x" }),
      frame({ type: "message.received", conversation_id: "c1", message_id: 3 }),
      frame({ type: "message.received", conversation_id: "c1", message_id: "a b" }),
      frame({ type: "message.received", conversation_id: "c1", pad: "x".repeat(MAX_FRAME) }),
    ]) {
      expect(parseInboxEvent(bad)).toBeNull();
    }
  });
});

describe("connectInbox", () => {
  it("entrega los eventos válidos y salta los demás sin cortar la conexión", () => {
    const { events } = start();
    last().open();
    last().say("{roto");
    last().say(JSON.stringify({ type: "otro", conversation_id: "c1" }));
    last().say(JSON.stringify({ type: "message.received", conversation_id: "c1" }));
    expect(events).toEqual([{ type: "message.received", conversation_id: "c1" }]);
    expect(last().closed).toBe(false);
  });

  it("la primera conexión no pide releer; cada reconexión, una vez", () => {
    const { resync } = start();
    last().open();
    expect(resync).not.toHaveBeenCalled();
    last().drop();
    vi.advanceTimersByTime(1000);
    expect(Fake.all).toHaveLength(2);
    expect(resync).not.toHaveBeenCalled(); // aún no abre
    last().open();
    expect(resync).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(STABLE); // una conexión estable
    last().drop();
    vi.advanceTimersByTime(1000);
    last().open();
    expect(resync).toHaveBeenCalledTimes(2);
  });

  it("espera cada vez más entre intentos, con tope, y vuelve a empezar al conectar", () => {
    expect([0, 1, 2, 3, 4, 5, 6, 7].map(backoff)).toEqual([
      1000, 2000, 4000, 8000, 16_000, 30_000, 30_000, 30_000,
    ]);
    start();
    for (const wait of [1000, 2000, 4000]) {
      last().drop();
      vi.advanceTimersByTime(wait - 1);
      expect(Fake.all.length).toBe(Fake.all.indexOf(last()) + 1); // todavía no
      vi.advanceTimersByTime(1);
    }
    expect(Fake.all).toHaveLength(4);
    last().open(); // conectó y se mantuvo: la espera vuelve a 1 s
    vi.advanceTimersByTime(STABLE);
    last().drop();
    vi.advanceTimersByTime(1000);
    expect(Fake.all).toHaveLength(5);
  });

  it("un intento que no llega a abrir sigue reintentando con la espera creciente", () => {
    start();
    last().drop(); // sin abrir nunca
    vi.advanceTimersByTime(1000);
    last().drop();
    vi.advanceTimersByTime(1999);
    expect(Fake.all).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(Fake.all).toHaveLength(3);
  });

  it("un servidor que acepta y cierra enseguida no se martillea: la espera sigue creciendo", () => {
    const { resync } = start();
    last().open();
    for (const wait of [1000, 2000, 4000]) {
      last().drop(1011); // abrió y se cayó al instante
      vi.advanceTimersByTime(wait - 1);
      expect(Fake.all.length).toBe(Fake.all.indexOf(last()) + 1);
      vi.advanceTimersByTime(1);
      last().open();
    }
    expect(Fake.all).toHaveLength(4);
    expect(resync).toHaveBeenCalledTimes(3); // una vez por reconexión, a ritmo decreciente
  });

  it("si la primera conexión falló, al abrir por fin relee (pudo perderse lo de antes)", () => {
    const { resync } = start();
    last().drop(); // nunca abrió
    vi.advanceTimersByTime(1000);
    last().open();
    expect(resync).toHaveBeenCalledTimes(1);
  });

  it("no reconecta tras un cierre definitivo del servidor", () => {
    for (const code of [4401, 4403, 4404]) {
      Fake.all = [];
      start();
      last().drop(code);
      vi.advanceTimersByTime(120_000);
      expect(Fake.all).toHaveLength(1);
    }
  });

  it("al parar cierra la conexión, cancela el reintento y no entrega nada más", () => {
    const { events, resync, stop } = start();
    last().open();
    const first = last();
    stop();
    expect(first.closed).toBe(true);
    first.say(JSON.stringify({ type: "message.received", conversation_id: "c1" }));
    first.drop();
    vi.advanceTimersByTime(60_000);
    expect(Fake.all).toHaveLength(1);
    expect(events).toEqual([]);
    expect(resync).not.toHaveBeenCalled();
    // parar en plena espera también cancela el reintento
    const again = start();
    last().drop();
    again.stop();
    vi.advanceTimersByTime(60_000);
    expect(Fake.all).toHaveLength(2);
  });

  it("un error solo no reconecta: lo hace el cierre que le sigue", () => {
    start();
    last().onerror?.({});
    vi.advanceTimersByTime(60_000);
    expect(Fake.all).toHaveLength(1);
  });
});

describe("robustez del cliente", () => {
  it("la espera lleva jitter entre el 50 % y el 100 %", () => {
    expect(jittered(4000, () => 0)).toBe(2000);
    expect(jittered(4000, () => 0.5)).toBe(3000);
    expect(jittered(4000, () => 1)).toBe(4000);
    connectInbox({
      url: "ws://x/",
      onEvent: () => {},
      onResync: () => {},
      create: (url) => new Fake(url),
      random: () => 0,
    });
    last().drop();
    vi.advanceTimersByTime(499);
    expect(Fake.all).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(Fake.all).toHaveLength(2);
  });

  it("si crear el socket lanza, reintenta como tras un cierre y relee al abrir", () => {
    let fail = true;
    const resync = vi.fn();
    connectInbox({
      url: "ws://x/",
      onEvent: () => {},
      onResync: resync,
      create: (url) => {
        if (fail) throw new Error("SecurityError");
        return new Fake(url);
      },
      random: () => 1,
    });
    expect(Fake.all).toHaveLength(0);
    vi.advanceTimersByTime(1000);
    expect(Fake.all).toHaveLength(0); // sigue lanzando: espera 2 s
    fail = false;
    vi.advanceTimersByTime(2000);
    expect(Fake.all).toHaveLength(1);
    last().open();
    expect(resync).toHaveBeenCalledTimes(1);
  });

  it("un cierre repetido de un socket viejo no programa otro intento", () => {
    start();
    const old = last();
    old.drop();
    vi.advanceTimersByTime(1000);
    expect(Fake.all).toHaveLength(2);
    old.drop(); // el viejo vuelve a avisar mientras el nuevo sigue conectando
    old.drop();
    vi.advanceTimersByTime(120_000);
    expect(Fake.all).toHaveLength(2);
  });
});

describe("latido", () => {
  it("manda un ping cada 25 s mientras está abierta y deja de mandarlo al cerrarse o al parar", () => {
    const { stop } = start();
    const socket = last();
    vi.advanceTimersByTime(PING_EVERY * 2); // sin abrir todavía: nada
    expect(socket.sent).toEqual([]);
    socket.open();
    vi.advanceTimersByTime(PING_EVERY * 3);
    expect(socket.sent).toEqual(Array(3).fill('{"type":"ping"}'));
    socket.drop();
    vi.advanceTimersByTime(PING_EVERY * 2 - 1); // cerrada: no manda; la siguiente aún no abre
    expect(socket.sent).toHaveLength(3);
    stop();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("para el latido al parar con la conexión abierta y manda varios por debajo del plazo del servidor", () => {
    expect(PING_EVERY * 3).toBeLessThan(90_000); // el servidor cierra a los 90 s sin tramas
    const { stop } = start();
    last().open();
    stop();
    expect(vi.getTimerCount()).toBe(0);
  });
});
