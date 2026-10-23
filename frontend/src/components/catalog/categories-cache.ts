import type { InfiniteData, QueryClient, QueryKey } from "@tanstack/react-query";

import type { CategoryNode } from "./categories-tree";

// Lo que una acción de fila cuenta a la pantalla. `here`: el foco seguía en la fila, que se va.
// `stale`: la categoría ya no existía y el árbol se vuelve a pedir.
export type Report = (text: string, how: { here: boolean; stale?: boolean }) => void;

type Tree = InfiniteData<{ results: CategoryNode[]; next: string | null }>;

// Cambia una categoría en el árbol en caché, en todas sus páginas; `null` la quita. Una lectura
// en vuelo traería el árbol de antes: se cancela primero y, si era una relectura, se repite.
export async function rewrite(
  queryClient: QueryClient,
  listKey: QueryKey,
  id: string,
  change: (node: CategoryNode) => CategoryNode | null,
) {
  const walk = (nodes: CategoryNode[]): CategoryNode[] =>
    nodes.flatMap((node) => {
      const next = node.id === id ? change(node) : node;
      return next ? [{ ...next, children: walk(next.children) }] : [];
    });
  const rereading = queryClient.getQueryState(listKey)?.fetchStatus === "fetching";
  await queryClient.cancelQueries({ queryKey: listKey });
  queryClient.setQueryData<Tree>(listKey, (data) =>
    data
      ? { ...data, pages: data.pages.map((page) => ({ ...page, results: walk(page.results) })) }
      : data,
  );
  if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
}
