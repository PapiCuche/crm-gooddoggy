"use client";

import { type QueryKey, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { shouldRetry } from "@/app/providers";
import { everyPage } from "@/components/catalog/categories-tree";
import { isStepUpCancelled, useStepUp } from "@/components/security/use-step-up";
import { getPriceListMarginsListQueryKey, priceListMarginsList } from "@/lib/api/client";
import type { ListMargin } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

// El segundo factor vale 15 minutos (ADR-003 §4): los costos no se quedan más en el navegador.
export const COST_MINUTES = 15;
// Las dos rutas que traen un costo: los márgenes de una lista y los costos de una variante.
const COST_ROUTE = /\/pricing\/(price-lists\/[^/]+\/margins|variants\/[^/]+\/costs)\/$/;
export const isCostQuery = ({ queryKey }: { queryKey: QueryKey }) =>
  typeof queryKey[0] === "string" && COST_ROUTE.test(queryKey[0]);

// «Ver costos y margen» (F3-47): pide `…/margins/` de la lista elegida, con step-up, solo mientras
// el interruptor está activo y la persona tiene `product_cost.view`. Al apagarlo, a los 15 minutos
// o al salir de la pantalla, los costos se QUITAN de la caché de TanStack Query (`removeQueries`),
// no solo de la vista. Sin permiso no hay petición. La frontera de seguridad es la API.
export function useMargins(args: {
  slug: string;
  listId: string;
  productId: string;
  allowed: boolean;
}) {
  const { slug, listId, productId, allowed } = args;
  const queryClient = useQueryClient();
  const { withStepUp, dialog } = useStepUp();
  const [on, setOn] = useState(false);
  const [declined, setDeclined] = useState(false); // cerró el diálogo del código
  const deadline = useRef(0);
  const active = allowed && on;

  const off = useCallback(() => {
    setOn(false);
    queryClient.removeQueries({ predicate: isCostQuery });
  }, [queryClient]);
  const params = productId ? { product_id: productId } : {};
  const query = useQuery<ListMargin[], ApiError>({
    queryKey: [...getPriceListMarginsListQueryKey(slug, listId, params), "all"],
    queryFn: async ({ signal }) => {
      try {
        // Todas las páginas de la lista: un solo step-up basta para el resto.
        return await withStepUp(() =>
          everyPage((page) =>
            priceListMarginsList(slug, listId, { ...params, ...page }, { signal }),
          ),
        );
      } catch (error) {
        if (isStepUpCancelled(error)) {
          setDeclined(true);
          off(); // sin verificar no hay costos: el interruptor vuelve a apagado
        }
        throw error;
      }
    },
    enabled: active && !!listId,
    // Una cancelación no se repite sola: volvería a abrir el diálogo.
    retry: (failures, error) => !isStepUpCancelled(error) && shouldRetry(failures, error),
    gcTime: 0,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (!active) return;
    deadline.current = Date.now() + COST_MINUTES * 60_000;
    const timer = setTimeout(off, COST_MINUTES * 60_000);
    // Un portátil dormido atrasa el temporizador: al volver a la pestaña se comprueba la hora.
    const woke = () => Date.now() >= deadline.current && off();
    document.addEventListener("visibilitychange", woke);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", woke);
    };
  }, [active, off]);
  useEffect(
    () => () => queryClient.removeQueries({ predicate: isCostQuery }), // al salir de la pantalla
    [queryClient],
  );

  return {
    active,
    declined,
    dialog,
    query,
    rows: active ? query.data : undefined,
    toggle: (next: boolean) => {
      setDeclined(false);
      if (next) setOn(true);
      else off();
    },
  };
}
