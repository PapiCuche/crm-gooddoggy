"use client";

import { useState } from "react";

import { isStepUpCancelled, useStepUp } from "@/components/security/use-step-up";
import type { ApiError } from "@/lib/http";

// La escritura de una cuenta de canal: la acción se pasa en cada envío y no queda nada de ella en
// la caché de TanStack (con `useMutation` las variables, y con ellas el token, se guardarían en la
// mutación). La ruta de conectar y rotar pide un segundo factor reciente: `withStepUp` abre el
// diálogo del código y repite la acción UNA vez, con lo mismo que ya tenía en su cierre; al
// terminar (bien, mal o cancelada) el cierre se suelta con la promesa.
export function useChannelWrite() {
  const { withStepUp, dialog } = useStepUp();
  const [state, setState] = useState<{ pending: boolean; error: ApiError | null }>({
    ...{ pending: false, error: null },
  });
  function run(action: () => Promise<unknown>, done: () => void) {
    setState({ pending: true, error: null });
    withStepUp(action).then(
      () => {
        setState({ pending: false, error: null });
        done();
      },
      // Cancelar el código no es un fallo: el formulario sigue como estaba.
      (error: ApiError) =>
        setState({ pending: false, error: isStepUpCancelled(error) ? null : error }),
    );
  }
  const write = {
    ...{ isPending: state.pending, isError: !!state.error, error: state.error },
    reset: () => state.error && setState({ pending: false, error: null }),
  };
  return { write, run, stepUp: dialog };
}
