"use client";

import { useTranslations } from "next-intl";
import { type KeyboardEvent, useEffect, useId, useRef, useState } from "react";

import type { Opportunity, Stage } from "@/lib/api/model";

import type { Mover } from "./move-opportunity";

type Held = {
  card: Opportunity;
  over: string | null; // la etapa que recibiría la tarjeta si se suelta ahora
  by: "pointer" | "keyboard";
  x: number;
  y: number;
  moved: boolean; // el puntero ya se movió: un clic sin movimiento no es un arrastre
};
const SLOP = 4; // píxeles que separan un clic de un arrastre

// Arrastrar una tarjeta a otra etapa (F5-05e), con el puntero o con el teclado. No mueve nada por
// sí mismo: al soltar llama a `mover.move` (F5-05b), que es quien envía, deshace y avisa. Las
// columnas son las que el tablero enseña; solo las etapas abiertas y activas distintas de la actual
// reciben la tarjeta. Con el puntero: se agarra el asa (que no deja a la página desplazarse bajo
// el dedo), la columna bajo el puntero se resalta, y soltar fuera de una válida, `Escape` o un
// cancel lo deshacen sin enviar nada. Con el teclado: Espacio/Intro toma la tarjeta, las flechas
// eligen la etapa (saltando las no válidas), Espacio/Intro suelta y `Escape` cancela. Lo que se
// dice en cada paso va a una región de estado.
export function useDragMove(mover: Mover, columns: readonly Stage[]) {
  const t = useTranslations("deals.drag");
  const [held, setHeld] = useState<Held | null>(null);
  const [said, setSaid] = useState("");
  const now = useRef<Held | null>(null); // lo último, también dentro de un manejador anterior
  const set = (next: Held | null) => {
    now.current = next;
    setHeld(next);
  };
  const isTarget = (stage: Stage, card: Opportunity) =>
    stage.is_active && stage.stage_type === "OPEN" && stage.id !== card.stage.id;
  const nameOf = (id: string | null) => columns.find((stage) => stage.id === id)?.name ?? "";

  function pick(card: Opportunity, by: Held["by"], x = 0, y = 0) {
    if (now.current || card.status !== "OPEN" || mover.pending.has(card.id)) return;
    set({ card, over: null, by, x, y, moved: false });
    setSaid(by === "keyboard" ? t("picked", { title: card.title, stage: card.stage.name }) : "");
  }
  function cancel(silent = false) {
    const current = now.current;
    if (!current) return;
    set(null);
    if (!silent)
      setSaid(t("cancelled", { title: current.card.title, stage: current.card.stage.name }));
  }
  function point(x: number, y: number) {
    const current = now.current;
    if (!current || current.by !== "pointer") return;
    const id = document.elementFromPoint?.(x, y)?.closest<HTMLElement>("[data-stage]")
      ?.dataset.stage;
    const target = columns.find((stage) => stage.id === id);
    const over = target && isTarget(target, current.card) ? target.id : null;
    const moved = current.moved || Math.hypot(x - current.x, y - current.y) > SLOP;
    if (over !== current.over) setSaid(over ? t("over", { stage: nameOf(over) }) : "");
    else if (moved && !current.moved) setSaid(t("grabbed", { title: current.card.title }));
    set({ ...current, over, moved, ...(moved ? { x, y } : {}) });
  }
  function drop() {
    const current = now.current;
    if (!current) return;
    const stage = columns.find(({ id }) => id === current.over);
    if (!stage) return cancel(current.by === "pointer" && !current.moved);
    set(null);
    setSaid(t("dropped", { title: current.card.title, stage: stage.name }));
    void mover.move(current.card, stage);
  }
  // Flecha: la etapa válida siguiente en esa dirección, desde la elegida (o desde la actual).
  function step(direction: 1 | -1) {
    const current = now.current;
    if (!current) return;
    let index = columns.findIndex(({ id }) => id === (current.over ?? current.card.stage.id));
    do index += direction;
    while (columns[index] && !isTarget(columns[index]!, current.card));
    const next = columns[index];
    if (!next) return setSaid(t("end"));
    set({ ...current, over: next.id });
    setSaid(t("over", { stage: next.name }));
  }
  function key(event: KeyboardEvent, card: Opportunity) {
    const keys: Record<string, () => void> = {
      Enter: () => (now.current ? drop() : pick(card, "keyboard")),
      " ": () => (now.current ? drop() : pick(card, "keyboard")),
      ArrowRight: () => step(1),
      ArrowLeft: () => step(-1),
    };
    if (!keys[event.key] || event.nativeEvent.isComposing) return;
    event.preventDefault();
    keys[event.key]!();
  }

  useEffect(() => {
    if (!held) return;
    const onKey = (event: globalThis.KeyboardEvent) => event.key === "Escape" && cancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  return {
    held,
    pick,
    point,
    drop,
    cancel,
    key,
    isTarget: (stage: Stage) => !!held && isTarget(stage, held.card),
    // Lo que se dice en cada paso, y la vista previa que sigue al puntero.
    live: (
      <>
        <p role="status" className="sr-only">
          {said}
        </p>
        {held?.by === "pointer" && held.moved ? (
          <div
            aria-hidden
            style={{ left: held.x, top: held.y }}
            className="border-foreground/40 bg-surface pointer-events-none fixed z-50 max-w-60 -translate-x-1/2 -translate-y-1/2 truncate rounded-[10px] border px-3 py-2 text-sm font-medium shadow-md"
          >
            {held.card.title}
          </div>
        ) : null}
      </>
    ),
  };
}
export type Drag = ReturnType<typeof useDragMove>;

// El asa de una tarjeta abierta: un botón con el nombre de la tarjeta, que se agarra con el
// puntero (`touch-none`: el dedo sobre el asa arrastra, no desplaza la página; fuera del asa, la
// tarjeta se desplaza como siempre) o se toma con el teclado. Si la tarjeta tiene una orden en
// curso, se ignora.
export function DragHandle({ card, drag, mover }: { card: Opportunity; drag: Drag; mover: Mover }) {
  const t = useTranslations("deals.drag");
  const hint = useId();
  const busy = mover.pending.has(card.id);
  const mine = drag.held?.card.id === card.id;
  if (card.status !== "OPEN") return null;
  return (
    <>
      <button
        type="button"
        aria-label={t("handle", { title: card.title })}
        aria-describedby={hint}
        aria-pressed={mine}
        aria-disabled={busy}
        className="border-border text-muted hover:bg-surface-raised aria-pressed:bg-accent aria-pressed:text-accent-foreground flex size-11 shrink-0 cursor-grab touch-none items-center justify-center rounded-lg border select-none aria-disabled:cursor-not-allowed aria-disabled:opacity-50 sm:size-9"
        onPointerDown={(event) => {
          if (busy || event.button > 0) return;
          event.currentTarget.setPointerCapture?.(event.pointerId);
          drag.pick(card, "pointer", event.clientX, event.clientY);
        }}
        onPointerMove={(event) => drag.point(event.clientX, event.clientY)}
        onPointerUp={() => drag.drop()}
        onPointerCancel={() => drag.cancel()}
        onBlur={() => drag.held?.by === "keyboard" && mine && drag.cancel()}
        onKeyDown={(event) => busy || drag.key(event, card)}
      >
        <svg viewBox="0 0 16 16" aria-hidden className="size-4" fill="currentColor">
          {[4, 8, 12].flatMap((y) =>
            [5, 11].map((x) => <circle key={`${x}-${y}`} cx={x} cy={y} r="1.2" />),
          )}
        </svg>
      </button>
      <span id={hint} className="sr-only">
        {t("hint")}
      </span>
    </>
  );
}
