"use client";

import { type RefObject, useEffect, useSyncExternalStore } from "react";

// Inbox en el teléfono (UX-INBOX-03).

// Se mira al pulsar, no al montar: un iPad con teclado físico puede declarar `pointer: fine`.
export const coarse = () =>
  typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
const never = () => () => {};
const desktop = () => false;
// Para lo que se pinta (ayuda, `enterKeyHint`): se relee en cada render; en el servidor, escritorio.
export const useCoarse = () => useSyncExternalStore(never, coarse, desktop);

// `?c=<id>`: solo un UUID llega a la API (el cliente no codifica la ruta: `../` no entra en ella).
export const validId = (id: string) =>
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id);

// Con `history` nativo: Next.js lo integra en su router (`useSearchParams` cambia, sin pedir la
// página al servidor) y «atrás» lo deshace.
export function setUrlConversation(id: string | null, push: boolean) {
  const url = new URL(location.href);
  if (id) url.searchParams.set("c", id);
  else url.searchParams.delete("c");
  history[push ? "pushState" : "replaceState"](null, "", url);
}

// iOS no encoge el viewport con el teclado (Android sí, con `interactiveWidget`): mientras se
// escribe, `--keyboard-vh` es el alto visible. Si se leía lo último, sigue a la vista al encoger.
export function useKeyboardHeight(
  panel: RefObject<HTMLElement | null>,
  scroller: RefObject<HTMLElement | null>,
) {
  useEffect(() => {
    const root = panel.current;
    const box = scroller.current;
    const viewport = window.visualViewport;
    if (!root || !box || !viewport) return;
    let pinned = true;
    const mark = () => (pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 8);
    const keep = new ResizeObserver(() => pinned && (box.scrollTop = box.scrollHeight));
    const fit = () =>
      root.style.setProperty("--keyboard-vh", `${Math.round(viewport.height * viewport.scale)}px`);
    const start = ({ target }: FocusEvent) => {
      if (!(target instanceof HTMLTextAreaElement)) return;
      fit();
      viewport.addEventListener("resize", fit);
    };
    const stop = () => {
      viewport.removeEventListener("resize", fit);
      root.style.removeProperty("--keyboard-vh");
    };
    keep.observe(box);
    box.addEventListener("scroll", mark);
    root.addEventListener("focusin", start);
    root.addEventListener("focusout", stop);
    return () => {
      keep.disconnect();
      box.removeEventListener("scroll", mark);
      root.removeEventListener("focusin", start);
      root.removeEventListener("focusout", stop);
      stop();
    };
  }, [panel, scroller]);
}
