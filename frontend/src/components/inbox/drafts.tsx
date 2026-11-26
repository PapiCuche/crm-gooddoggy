"use client";

import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";

import type { Mode } from "./composer";

// El intento (id, primer texto y si llevaba archivo, que no se guarda) y si hay un envío en curso.
// `unsure`: su archivo venció sin respuesta y pudo llegar (UX-INBOX-04c).
type Attempt = { id: string; first: string; media: boolean; unsure?: boolean };
// El toque de «Enviar» en curso (UX-INBOX-06f): `since`, su hora de reloj (`Date.now()`); `started`,
// la petición ya salió y desde ahí no se cancela ni caduca.
export type Press = { id: string; since: number; started?: boolean };
export type Draft = {
  text: string;
  attempt: Attempt | null;
  sending: boolean;
  press?: Press | null;
};
const EMPTY: Draft = { text: "", attempt: null, sending: false };
const MODES: readonly Mode[] = ["reply", "note"];

// Borradores de una organización (UX-INBOX-04), solo en la memoria de la pestaña. Clave: organización,
// conversación y modo. Cerrado, se vacía y no acepta lo que llegue después (un envío que termina).
export class Drafts {
  #rows = new Map<string, Draft>();
  #listeners = new Set<() => void>();
  #open = true;
  #unsent = new Set<string>(); // conversaciones con respuesta o nota sin enviar (UX-INBOX-04b)
  constructor(readonly organization: string) {}

  key = (conversation: string, mode: Mode) => `${this.organization}/${conversation}/${mode}`;
  #own = (key: string) => key.startsWith(`${this.organization}/`); // los ids son UUID, sin «/»

  get = (key: string): Draft => this.#rows.get(key) ?? EMPTY;
  // Lo calcula `update` para su conversación: cada fila, en cada tecla, solo mira un `Set`.
  has = (conversation: string) => this.#unsent.has(conversation);

  // Cambia el borrador de esa clave a partir del que hay ahora (nunca de una copia vieja).
  update = (key: string, change: (draft: Draft) => Partial<Draft>) => {
    if (!this.#open || !this.#own(key)) return;
    const next = { ...this.get(key), ...change(this.get(key)) };
    if (next.text === "" && !next.attempt && !next.sending) this.#rows.delete(key);
    else this.#rows.set(key, next);
    // Respuesta o nota con texto sin enviar; un modo con un envío en curso no cuenta (lo que sale).
    const conversation = key.slice(this.organization.length + 1, key.lastIndexOf("/"));
    const unsent = MODES.some((mode) => {
      const { text, sending } = this.get(this.key(conversation, mode));
      return text.trim() !== "" && !sending;
    });
    if (unsent) this.#unsent.add(conversation);
    else this.#unsent.delete(conversation);
    this.#listeners.forEach((listener) => listener());
  };

  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => void this.#listeners.delete(listener);
  };

  open = () => void (this.#open = true);
  close = () => {
    this.#open = false;
    this.#rows.clear();
    this.#unsent.clear();
    this.#listeners.forEach((listener) => listener());
  };
}

const Context = createContext<Drafts | null>(null);
type Props = { organization: string; children: ReactNode };

// Va dentro de `TenantProvider`, con `key` de la organización y la persona: otra monta uno nuevo.
export function DraftsProvider({ organization, children }: Props) {
  const [drafts] = useState(() => new Drafts(organization));
  useEffect(() => {
    drafts.open(); // en desarrollo React desmonta y vuelve a montar: se reabre
    return drafts.close;
  }, [drafts]);
  return <Context.Provider value={drafts}>{children}</Context.Provider>;
}

// Lo que se lee del almacén, al día con cada cambio; `(all) => all` da el almacén.
export function useDraftsValue<T>(pick: (drafts: Drafts) => T) {
  const drafts = useContext(Context);
  if (!drafts) throw new Error("useDraftsValue se usa dentro de una ruta de organización");
  const read = () => pick(drafts);
  return useSyncExternalStore(drafts.subscribe, read, read);
}
