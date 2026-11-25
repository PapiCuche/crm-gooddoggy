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
type Attempt = { id: string; first: string; media: boolean };
export type Draft = { text: string; attempt: Attempt | null; sending: boolean };
const EMPTY: Draft = { text: "", attempt: null, sending: false };

// Borradores de una organización (UX-INBOX-04), solo en la memoria de la pestaña. Clave: organización,
// conversación y modo. Cerrado, se vacía y no acepta lo que llegue después (un envío que termina).
export class Drafts {
  #rows = new Map<string, Draft>();
  #listeners = new Set<() => void>();
  #open = true;
  constructor(readonly organization: string) {}

  key = (conversation: string, mode: Mode) => `${this.organization}/${conversation}/${mode}`;
  #own = (key: string) => key.startsWith(`${this.organization}/`); // los ids son UUID, sin «/»

  get = (key: string): Draft => this.#rows.get(key) ?? EMPTY;

  // Cambia el borrador de esa clave a partir del que hay ahora (nunca de una copia vieja).
  update = (key: string, change: (draft: Draft) => Partial<Draft>) => {
    if (!this.#open || !this.#own(key)) return;
    const next = { ...this.get(key), ...change(this.get(key)) };
    if (next.text === "" && !next.attempt && !next.sending) this.#rows.delete(key);
    else this.#rows.set(key, next);
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
