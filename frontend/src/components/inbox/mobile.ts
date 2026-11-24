"use client";

import { type RefObject, useEffect, useSyncExternalStore } from "react";

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

// `history` nativo: Next.js lo integra en su router (`useSearchParams`) sin pedir la página.
export function setUrlConversation(id: string | null, push: boolean) {
  const url = new URL(location.href);
  if (id) url.searchParams.set("c", id);
  else url.searchParams.delete("c");
  history[push ? "pushState" : "replaceState"](null, "", url);
}

type Box = RefObject<HTMLElement | null>;
// iOS no encoge el viewport con el teclado (Android sí): mientras se escribe, `--keyboard-vh`.
export function useKeyboardHeight(panel: Box, scroller: Box) {
  useEffect(() => {
    const [root, box, viewport] = [panel.current, scroller.current, window.visualViewport];
    if (!root || !box || !viewport) return;
    let pinned = true; // se leía lo último: sigue a la vista al encoger
    const mark = () => (pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 8);
    const follow = () => pinned && (box.scrollTop = box.scrollHeight);
    const keep = window.ResizeObserver ? new ResizeObserver(follow) : null; // jsdom no lo trae
    const fit = () =>
      root.style.setProperty("--keyboard-vh", `${Math.round(viewport.height * viewport.scale)}px`);
    const start = ({ target }: FocusEvent) =>
      target instanceof HTMLTextAreaElement && (fit(), viewport.addEventListener("resize", fit));
    const stop = () => {
      viewport.removeEventListener("resize", fit);
      root.style.removeProperty("--keyboard-vh");
    };
    const off = new AbortController();
    const options = { signal: off.signal };
    box.addEventListener("scroll", mark, options);
    root.addEventListener("focusin", start, options);
    root.addEventListener("focusout", stop, options);
    keep?.observe(box);
    return () => {
      off.abort();
      keep?.disconnect();
      stop();
    };
  }, [panel, scroller]);
}
