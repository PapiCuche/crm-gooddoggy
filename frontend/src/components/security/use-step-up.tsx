"use client";

import { type ReactElement, useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/http";

import { StepUpDialog } from "./step-up-dialog";

export const STEP_UP_REQUIRED = "STEP_UP_REQUIRED";

// Lo que rechaza `withStepUp` si la persona cancela (o el componente se desmonta) con el
// diálogo abierto: la acción no se hizo y la pantalla sigue como estaba. No es un fallo.
export class StepUpCancelled extends Error {
  constructor() {
    super("STEP_UP_CANCELLED");
    this.name = "StepUpCancelled";
  }
}
export const isStepUpCancelled = (error: unknown) => error instanceof StepUpCancelled;

const isStepUpRequired = (error: unknown) =>
  error instanceof ApiError && error.status === 403 && error.code === STEP_UP_REQUIRED;

type Waiting = { promise: Promise<void>; resolve: () => void; reject: (error: Error) => void };

// Step-up sin proveedor (ADR-003 §4, ADR-023 §7): quien lo usa pinta `dialog` en su árbol y
// envuelve la acción sensible en `withStepUp`. Si responde 403 `STEP_UP_REQUIRED`, se abre el
// diálogo; con el código bueno se repite la acción UNA vez. Si esa segunda vez también pide
// step-up (la sesión cambió, el servidor lo decide), el error sale tal cual: no hay bucle.
//   const { withStepUp, dialog } = useStepUp();
//   await withStepUp(() => save.mutateAsync(data));   // rechaza con StepUpCancelled al cancelar
export function useStepUp(): {
  withStepUp: <T>(run: () => Promise<T>) => Promise<T>;
  dialog: ReactElement | null;
} {
  const [open, setOpen] = useState(false);
  const waiting = useRef<Waiting | null>(null);

  const settle = useCallback((verified: boolean) => {
    const now = waiting.current;
    if (!now) return;
    waiting.current = null;
    setOpen(false);
    if (verified) now.resolve();
    else now.reject(new StepUpCancelled());
  }, []);
  useEffect(() => () => settle(false), [settle]);

  const ask = useCallback(() => {
    if (!waiting.current) {
      let resolve!: () => void;
      let reject!: (error: Error) => void;
      const promise = new Promise<void>((yes, no) => {
        resolve = yes;
        reject = no;
      });
      waiting.current = { promise, resolve, reject };
      setOpen(true);
    }
    return waiting.current.promise; // dos acciones a la vez comparten un solo diálogo
  }, []);

  const withStepUp = useCallback(
    async <T,>(run: () => Promise<T>): Promise<T> => {
      try {
        return await run();
      } catch (error) {
        if (!isStepUpRequired(error)) throw error;
      }
      await ask();
      return run();
    },
    [ask],
  );

  const dialog = open ? (
    <StepUpDialog onVerified={() => settle(true)} onCancel={() => settle(false)} />
  ) : null;
  return { withStepUp, dialog };
}
