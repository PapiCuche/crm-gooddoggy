// El cliente WebSocket del Inbox (F6-07c), sin React: el contrato con el servicio `ws` (F6-07a) y la
// reconexión. El servidor manda texto JSON `{type, conversation_id, message_id?}` por
// `/ws/o/{slug}/inbox/`, autenticado por la cookie de sesión (mismo origen, ADR-003). Aquí solo se
// valida lo que llega, se reconecta con espera creciente y se avisa de cada evento válido y de cada
// reconexión (cuando se pudo perder algo). Qué hacer con ellos es de quien lo usa.

export const INBOX_EVENTS = [
  "message.received",
  "message.updated",
  "conversation.updated",
] as const;
export type InboxEvent = {
  type: (typeof INBOX_EVENTS)[number];
  conversation_id: string;
  message_id?: string;
};

// Cierres que el servidor da a propósito y que reconectar no arregla (`core.tenancy.channels`:
// 4403 organización suspendida, 4404 sin organización o sin membresía; 4401, sin sesión).
export const FINAL_CLOSES: readonly number[] = [4401, 4403, 4404];
const ID = /^[0-9a-zA-Z-]{1,64}$/;
// Un fotograma legítimo mide unas decenas de bytes: uno mayor no se analiza (no se bloquea la
// pantalla con un JSON enorme).
export const MAX_FRAME = 4096;

// Un fotograma que no es lo acordado (binario, JSON roto, tipo desconocido, ids raros) se ignora:
// el servidor puede crecer con tipos nuevos sin romper a un cliente viejo.
export function parseInboxEvent(data: unknown): InboxEvent | null {
  if (typeof data !== "string" || data.length > MAX_FRAME) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof raw !== "object" || raw === null) return null;
  const {
    type,
    conversation_id: conversation,
    message_id: message,
  } = raw as Record<string, unknown>;
  if (!INBOX_EVENTS.includes(type as InboxEvent["type"])) return null;
  if (typeof conversation !== "string" || !ID.test(conversation)) return null;
  if (message !== undefined && (typeof message !== "string" || !ID.test(message))) return null;
  return {
    type: type as InboxEvent["type"],
    conversation_id: conversation,
    ...(message === undefined ? {} : { message_id: message }),
  };
}

// Lo que se usa del `WebSocket` (también lo cumple el doble de las pruebas).
export type SocketLike = {
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close: () => void;
};

const WAIT = { first: 1000, max: 30_000 };
// Una conexión que dura menos que esto no cuenta como recuperada: si el servidor acepta y cierra
// enseguida, la espera sigue creciendo (y no se relee todo el Inbox cada segundo).
export const STABLE = 10_000;

// La espera antes del intento `attempt` (0 el primero): 1 s, 2 s, 4 s… con tope de 30 s.
export const backoff = (attempt: number) => Math.min(WAIT.max, WAIT.first * 2 ** attempt);

// Con jitter: entre la mitad y el total de la espera, para que muchos clientes que perdieron el
// servidor a la vez no vuelvan todos en el mismo instante.
export const jittered = (wait: number, random: () => number) => wait * (0.5 + 0.5 * random());

export function connectInbox({
  url,
  onEvent,
  onResync,
  create = (target: string) => new WebSocket(target) as unknown as SocketLike,
  random = Math.random,
}: {
  url: string;
  onEvent: (event: InboxEvent) => void;
  onResync: () => void; // se volvió a conectar tras un corte: lo ocurrido mientras tanto se relee
  create?: (url: string) => SocketLike;
  random?: () => number;
}): () => void {
  let socket: SocketLike | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  let lost = false; // ¿se perdió una conexión o un intento? lo ocurrido mientras tanto se relee
  let since = 0; // cuándo abrió la conexión actual (0: no está abierta)
  let stopped = false;

  // Tras perder la conexión (o un intento): se relee al volver y se espera más cada vez.
  function retry() {
    since = 0;
    lost = true;
    timer = setTimeout(open, jittered(backoff(attempt++), random));
  }

  function open() {
    let current: SocketLike;
    try {
      current = create(url);
    } catch {
      retry(); // un `new WebSocket` que lanza (URL o política) cuenta como un cierre
      return;
    }
    socket = current;
    current.onopen = () => {
      since = Date.now();
      if (lost) onResync(); // la primera conexión sin fallos previos no relee: la pantalla acaba de leer
      lost = false;
    };
    current.onmessage = (frame) => {
      const event = parseInboxEvent(frame.data);
      if (event) onEvent(event);
    };
    current.onclose = (close) => {
      if (stopped || socket !== current) return;
      socket = null;
      if (since && Date.now() - since >= STABLE) attempt = 0; // estable: se cuenta desde cero
      if (FINAL_CLOSES.includes(close.code)) {
        since = 0;
        lost = true;
        return;
      }
      retry();
    };
    current.onerror = () => {}; // sigue un `close`, que es lo que trata la reconexión
  }
  open();

  return () => {
    stopped = true;
    clearTimeout(timer);
    const current = socket;
    socket = null;
    if (current) {
      current.onclose = current.onmessage = current.onopen = current.onerror = null;
      current.close();
    }
  };
}
