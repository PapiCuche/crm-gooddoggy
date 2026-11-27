import { type RefObject, useEffect, useState, useSyncExternalStore } from "react";
import { flushSync } from "react-dom";

// Se mira al pulsar, no al montar: un iPad con teclado físico puede declarar `pointer: fine`.
export const coarse = () =>
  typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
const never = () => () => {};
// Para lo que se pinta (ayuda, `enterKeyHint`): se relee en cada render; en el servidor, escritorio.
export const useCoarse = () => useSyncExternalStore(never, coarse, () => false);
// Por debajo de 640 px (`max-sm`), y al cruzarlo (girar el teléfono) se vuelve a pintar.
const COMPACT = "(max-width: 639px)";
const narrow = () => typeof matchMedia === "function" && matchMedia(COMPACT).matches;
const resized = (change: () => void) => {
  const query = typeof matchMedia === "function" ? matchMedia(COMPACT) : undefined;
  query?.addEventListener?.("change", change);
  return () => query?.removeEventListener?.("change", change);
};
export const useCompact = () => useSyncExternalStore(resized, narrow, () => false);

// `?c=<id>`: solo un UUID llega a la API (el cliente no codifica la ruta: `../` no entra en ella).
export const validId = (id: string) =>
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id);

// `history` nativo: Next.js lo integra en su router (`useSearchParams`) sin pedir la página.
export function setUrlConversation(id: string | null, push: boolean) {
  const url = new URL(location.href);
  if (id) url.searchParams.set("c", id);
  else url.searchParams.delete("c");
  history[push ? "pushState" : "replaceState"](null, "", url);
}

const CLOSED = 150; // px: más que la barra de direcciones (56), menos que cualquier teclado
// iOS no encoge el viewport con el teclado (Android sí): mientras se escribe, `--keyboard-vh`.
export function useKeyboardHeight(panel: RefObject<HTMLElement | null>, scroller: typeof panel) {
  useEffect(() => {
    const [root, box, viewport] = [panel.current, scroller.current, window.visualViewport];
    if (!root || !box || !viewport) return;
    let [pinned, seen] = [true, box.clientHeight]; // se leía lo último: sigue a la vista al encoger
    const mark = () =>
      box.clientHeight === seen &&
      (pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 8);
    const follow = () => ((seen = box.clientHeight), pinned && (box.scrollTop = box.scrollHeight));
    const keep = window.ResizeObserver ? new ResizeObserver(follow) : null; // jsdom no lo trae
    // Android cierra el teclado sin quitar el foco: si lo visible crece más que una barra de
    // direcciones desde lo menor visto (con el mismo ancho: girar no cuenta), se suelta el cuadro.
    let [typing, low, wide] = [null as HTMLTextAreaElement | null, 0, 0];
    const fit = () => {
      const seen = Math.round(viewport.height * viewport.scale);
      root.style.setProperty("--keyboard-vh", `${seen}px`);
      if (viewport.width !== wide) [low, wide] = [seen, viewport.width];
      low = Math.min(low, seen);
      if (seen - low > CLOSED && coarse()) typing?.blur();
    };
    const start = ({ target }: FocusEvent) =>
      target instanceof HTMLTextAreaElement &&
      ((typing = target), (wide = 0), fit(), viewport.addEventListener("resize", fit));
    const stop = () => {
      viewport.removeEventListener("resize", fit);
      root.style.removeProperty("--keyboard-vh");
    };
    const off = new AbortController();
    box.addEventListener("scroll", mark, { signal: off.signal });
    root.addEventListener("focusin", start, { signal: off.signal });
    root.addEventListener("focusout", stop, { signal: off.signal });
    keep?.observe(box);
    return () => {
      off.abort();
      keep?.disconnect();
      stop();
    };
  }, [panel, scroller]);
}

// UX-INBOX-03b, solo táctil: del foco en un cuadro del redactor (lo último del panel) a su salida,
// tras un turno: el foco puede pasar a un botón del redactor (Chrome enfoca el que se toca).
export function useComposing(panel: RefObject<HTMLElement | null>) {
  const [on, setOn] = useState(false);
  useEffect(() => {
    const root = panel.current;
    if (!root) return;
    let wait = 0;
    const enter = ({ target }: FocusEvent) => {
      if (!(target instanceof Node && root.lastElementChild?.contains(target))) return; // el redactor
      clearTimeout(wait); // el foco sigue en el redactor
      if (target instanceof HTMLTextAreaElement && coarse()) setOn(true);
    };
    const leave = () => (clearTimeout(wait), (wait = window.setTimeout(() => setOn(false))));
    const off = new AbortController();
    root.addEventListener("focusin", enter, { signal: off.signal });
    root.addEventListener("focusout", leave, { signal: off.signal });
    return () => (clearTimeout(wait), off.abort());
  }, [panel]);
  // UX-INBOX-03c: compacto (táctil y por debajo de 640 px, como `max-sm`), pulsar un botón del panel
  // no quita el foco al cuadro. Si lo quitara, la cabecera y `--keyboard-vh` cambiarían entre
  // `mousedown` y el clic (WebKit lo perdía). En una tableta, o con el foco fuera del cuadro (p. ej.,
  // en el buscador de respuestas rápidas), el foco se mueve como siempre.
  useEffect(() => {
    const root = panel.current;
    if (!root || !on) return;
    const keep = (event: MouseEvent) => {
      const [typing, press] = [document.activeElement, event.target];
      if (!(press instanceof Element && press.closest("button"))) return;
      if (!matchMedia("(max-width: 639px)").matches) return;
      if (typing instanceof HTMLTextAreaElement && root.lastElementChild?.contains(typing))
        event.preventDefault();
    };
    root.addEventListener("mousedown", keep);
    return () => root.removeEventListener("mousedown", keep);
  }, [panel, on]);
  return [on, () => flushSync(() => setOn(false))] as const;
}
