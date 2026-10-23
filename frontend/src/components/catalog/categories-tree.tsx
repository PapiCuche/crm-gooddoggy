"use client";

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { categoriesList, getCategoriesListQueryKey } from "@/lib/api/client";
import type { Category } from "@/lib/api/model";
import { ApiError } from "@/lib/http";

import type { Report } from "./categories-cache";
import { CategoryCreate } from "./category-create";
import { CategoryDeleteAction } from "./category-delete-action";
import { CategoryEditAction } from "./category-edit-action";

export type CategoryNode = Category & { children: CategoryNode[] };

// Todas las filas de un listado (el árbol se arma entero; un `select`, también): se sigue el
// cursor hasta el final.
export async function everyPage<Row>(
  fetchPage: (params: {
    limit: number;
    cursor?: string;
  }) => Promise<{ results: Row[]; next?: string | null }>,
): Promise<Row[]> {
  const found: Row[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await fetchPage({ limit: 200, ...(cursor ? { cursor } : {}) });
    found.push(...page.results);
    cursor = page.next ?? undefined;
    // Un cursor repetido no avanza: es un fallo de la API, no una lista sin fin.
    if (cursor && seen.has(cursor)) throw new ApiError(500, "INTERNAL_ERROR");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return found;
}

// Por `path` (los slugs de la rama): cada categoría va después de su madre y antes de la
// hermana siguiente. Sin `localeCompare`: el orden de los caracteres, no el del idioma.
export function grow(found: Category[]): CategoryNode[] {
  const sorted = [...found].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  const nodes = new Map(sorted.map((row) => [row.id, { ...row, children: [] as CategoryNode[] }]));
  const roots: CategoryNode[] = [];
  for (const node of nodes.values()) {
    (nodes.get(node.parent_id ?? "")?.children ?? roots).push(node);
  }
  return roots;
}
export const flatten = (nodes: CategoryNode[]): CategoryNode[] =>
  nodes.flatMap((node) => [node, ...flatten(node.children)]);

type Actions = Omit<Parameters<typeof CategoryDeleteAction>[0], "category"> & {
  all: CategoryNode[];
};

// Una categoría y, debajo, sus hijas en otra lista: el anidamiento es el del árbol. Con
// `actions` (`categories.manage`), cada una se puede editar y borrar.
function Branch({ node, actions }: { node: CategoryNode; actions?: Actions }) {
  const t = useTranslations("categories");
  return (
    <>
      <div className="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
        <p className="flex min-w-0 flex-col leading-snug">
          <span className="font-medium wrap-anywhere">{node.name}</span>
          <span className="text-muted font-mono text-[13px] wrap-anywhere">{node.slug}</span>
        </p>
        {actions ? (
          // Abierta, una acción ocupa la fila: en la columna `auto` estrecharía el nombre.
          <div className="flex flex-wrap items-start gap-2 sm:has-[form,[role=group]]:col-span-2">
            <CategoryEditAction {...actions} category={node} />
            <CategoryDeleteAction {...actions} category={node} />
          </div>
        ) : null}
      </div>
      {node.children.length > 0 ? (
        <ul
          aria-label={t("inside", { name: node.name })}
          className="border-border mt-3 flex flex-col gap-3 border-s ps-3 sm:ps-4"
        >
          {node.children.map((child) => (
            <li key={child.id}>
              <Branch node={child} actions={actions} />
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

// Categorías del catálogo (F3-05): el árbol de `GET /api/v1/o/{slug}/categories/` (F3-04), con
// listas anidadas que un lector de pantalla recorre por niveles. Cada escritura vuelve a pedir
// el árbol entero. Quién puede verlo o cambiarlo lo decide la API (`products.view`,
// `categories.manage`).
export function CategoriesTree() {
  const t = useTranslations();
  const { organization, permissions } = useTenant();
  // Comodidad: las escrituras se ofrecen a quien la API dijo que tiene `categories.manage`.
  const canManage = permissions.some((grant) => grant.code === "categories.manage");
  const listKey = [...getCategoriesListQueryKey(organization.slug), "tree"];
  const list = useRef<CursorListHandle>(null);
  // Lo que se hizo, o que la categoría ya no estaba: aquí, porque la fila puede irse del árbol.
  const [notice, setNotice] = useState<{ text: string; stale: boolean } | null>(null);
  const report: Report = (text, { here, stale = false }) => {
    setNotice({ text, stale });
    if (stale) list.current?.refetch();
    if (here) list.current?.focusHeading();
  };
  const ask = () => setNotice(null);
  const done = notice && !notice.stale ? notice.text : null;

  return (
    <CursorList<CategoryNode>
      ref={list}
      section="categories"
      organization={organization.name}
      listKey={listKey}
      fetchPage={async (_cursor, signal) => ({
        results: grow(
          await everyPage((params) => categoriesList(organization.slug, params, { signal })),
        ),
        next: null,
      })}
      notice={(roots) => (
        <>
          {canManage ? (
            <CategoryCreate
              slug={organization.slug}
              all={flatten(roots)}
              listKey={listKey}
              onAsk={ask}
              onDone={(text) => report(text, { here: false })}
            />
          ) : null}
          {notice?.stale ? (
            <p role="alert" className="text-danger">
              {notice.text}
            </p>
          ) : null}
          {/* Montado desde el principio para quien puede escribir: se anuncia cuando cambia. */}
          {canManage ? (
            <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
              {done ?? ""}
            </p>
          ) : null}
        </>
      )}
      empty={<p className="text-muted">{t("categories.empty")}</p>}
    >
      {(root, roots) => (
        <Branch
          node={root}
          actions={
            canManage
              ? {
                  slug: organization.slug,
                  listKey,
                  all: flatten(roots),
                  onAsk: ask,
                  onDone: report,
                }
              : undefined
          }
        />
      )}
    </CursorList>
  );
}
