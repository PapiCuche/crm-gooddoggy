"use client";

import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";

// Espera tras la última tecla antes de preguntar a la API (F4-13).
export const SEARCH_DELAY = 300;
const MAX = 254; // el máximo de `q` en la API

// Lo que se pide a la API: el texto sin espacios en los extremos, si llega al mínimo (2
// caracteres, o 1 si empieza por «+» o es una cifra); si no, nada y la lista sigue entera.
export function searchTerm(text: string) {
  const term = text.trim();
  return Array.from(term).length >= (/^[+\d]/.test(term) ? 1 : 2) ? term : "";
}

// La búsqueda de Clientes. La URL (`?q=`) es la fuente de verdad, para recargar y compartir; la
// caja guarda un borrador que pasa a la URL tras la espera y, a la API, solo si llega al mínimo.
// Se escribe con `history.replaceState`: Next lo integra con `useSearchParams` sin pedir la
// página otra vez ni añadir una entrada al historial.
export function useContactSearch() {
  const fromUrl = useSearchParams()?.get("q") ?? "";
  const [draft, setDraft] = useState(fromUrl);
  // Lo último que esta pantalla escribió en la URL: un cambio de la URL que no es suyo (un
  // enlace, «Clientes» en el menú) sustituye el borrador; el suyo no pisa lo que se escribe después.
  const [pushed, setPushed] = useState(fromUrl);
  const [seen, setSeen] = useState(fromUrl);
  if (fromUrl !== seen) {
    setSeen(fromUrl);
    if (fromUrl !== pushed) {
      setPushed(fromUrl);
      setDraft(fromUrl);
    }
  }
  // Durante una composición (IME) el texto aún no es el definitivo: no se envía a medias.
  const [composing, setComposing] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  function commit(text: string) {
    setPushed(text);
    const params = new URLSearchParams(window.location.search);
    if (text) params.set("q", text);
    else params.delete("q");
    const query = params.toString();
    const { pathname, hash } = window.location;
    window.history.replaceState(null, "", `${pathname}${query ? `?${query}` : ""}${hash}`);
  }
  const wanted = draft.trim();
  useEffect(() => {
    if (composing || wanted === pushed) return;
    const timer = setTimeout(() => commit(wanted), SEARCH_DELAY);
    return () => clearTimeout(timer);
  }, [composing, wanted, pushed]);

  function clear() {
    setDraft("");
    commit("");
    input.current?.focus(); // el botón se va: el foco se queda en la caja
  }
  return {
    term: searchTerm(fromUrl),
    draft,
    short: wanted !== "" && searchTerm(wanted) === "",
    input,
    change: setDraft,
    compose: setComposing,
    clear,
  };
}

const CLEAR_ICON = (
  <svg viewBox="0 0 16 16" aria-hidden="true" className="size-4" fill="none" stroke="currentColor">
    <path d="M4 4l8 8M12 4l-8 8" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
);

// La caja: `type=search` con su etiqueta, 16 px (iOS no amplía la página) y 44 px de alto. El
// botón de borrar solo está con texto; «Escape» hace lo mismo. Lo escrito se pinta como texto.
export function ContactSearch({ search }: { search: ReturnType<typeof useContactSearch> }) {
  const t = useTranslations("contacts.search");
  const id = useId();
  const { draft, input, short } = search;
  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Escape" || draft === "") return;
    event.preventDefault();
    search.clear();
  }
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {t("label")}
      </label>
      <div className="relative">
        <input
          ref={input}
          id={id}
          type="search"
          value={draft}
          maxLength={MAX}
          autoComplete="off"
          spellCheck={false}
          enterKeyHint="search"
          aria-describedby={`${id}-hint`}
          className="bg-surface border-foreground/50 focus-visible:border-foreground h-11 w-full rounded-[10px] border pr-11 pl-3 text-[16px] [&::-webkit-search-cancel-button]:appearance-none"
          onChange={(event) => search.change(event.target.value)}
          onCompositionStart={() => search.compose(true)}
          onCompositionEnd={() => search.compose(false)}
          onKeyDown={onKeyDown}
        />
        {draft === "" ? null : (
          <Button
            type="button"
            size="icon"
            aria-label={t("clear")}
            className="absolute top-0 right-0 size-11"
            onClick={search.clear}
          >
            {CLEAR_ICON}
          </Button>
        )}
      </div>
      <p id={`${id}-hint`} className="text-muted text-sm">
        {short ? t("short") : t("hint")}
      </p>
    </div>
  );
}
