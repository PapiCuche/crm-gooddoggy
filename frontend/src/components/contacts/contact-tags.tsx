"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { everyPage } from "@/components/catalog/categories-tree";
import { getTagsListQueryKey, tagsList } from "@/lib/api/client";
import type { ContactTag } from "@/lib/api/model";
import { cn } from "@/lib/utils";

const HEX = /^#[0-9a-f]{6}$/i;
// Luminancia relativa de WCAG 2.x de un `#rrggbb`.
function luminance(color: string) {
  const [r, g, b] = [1, 3, 5].map((at) => {
    const channel = parseInt(color.slice(at, at + 2), 16) / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}
export const contrast = (one: string, other: string) => {
  const [light, dark] = [luminance(one), luminance(other)].sort((a, b) => b - a);
  return (light! + 0.05) / (dark! + 0.05);
};
// El texto sobre el color de una etiqueta: negro o blanco, el que más contraste da. Con los
// dos extremos, cualquier fondo llega al menos a 4,58:1 (AA para texto normal); la tinta del
// tema (#202020) se quedaría en 4,04:1 con los grises medios.
export const inkOn = (color: string) =>
  contrast(color, "#000000") >= contrast(color, "#ffffff") ? "#000000" : "#ffffff";

// Una etiqueta (F4-11): su nombre siempre escrito, sobre su color. El borde la separa del papel
// cuando el color es claro. Una desactivada sigue en el contacto, con borde discontinuo y
// «desactivada» escrito: no se dice solo con el color ni con el borde.
export function TagChip({ tag }: { tag: Pick<ContactTag, "name" | "color" | "is_active"> }) {
  const t = useTranslations("contacts.tags");
  const painted = HEX.test(tag.color); // la API siempre lo da así; si no, una etiqueta neutra
  return (
    <span
      className={cn(
        "border-foreground/20 inline-flex max-w-full items-center gap-1 rounded-full border px-2.5 py-0.5 text-[13px] leading-5 font-medium",
        !painted && "bg-surface text-foreground",
        !tag.is_active && "border-dashed",
      )}
      style={painted ? { backgroundColor: tag.color, color: inkOn(tag.color) } : undefined}
    >
      <span className="wrap-anywhere">{tag.name}</span>
      {tag.is_active ? null : <span className="font-normal">· {t("inactive")}</span>}
    </span>
  );
}

// Las etiquetas de un contacto, como lista: el lector de pantalla dice cuántas son.
export function TagList({ tags, className }: { tags: readonly ContactTag[]; className?: string }) {
  const t = useTranslations("contacts.tags");
  if (tags.length === 0) return null;
  return (
    <ul aria-label={t("label")} className={cn("flex flex-wrap gap-1.5", className)}>
      {tags.map((tag) => (
        <li key={tag.id} className="max-w-full">
          <TagChip tag={tag} />
        </li>
      ))}
    </ul>
  );
}

// El catálogo entero de etiquetas (`GET …/tags/`, con `contacts.view` y cualquier alcance),
// activas o no, por orden de creación. Lo leen el filtro de la lista y la ficha.
// `enabled`: solo si la API dijo que el usuario tiene `contacts.view`.
export function useTagCatalog(slug: string, enabled = true) {
  return useQuery({
    queryKey: [...getTagsListQueryKey(slug), "all"],
    queryFn: ({ signal }) => everyPage((params) => tagsList(slug, params, { signal })),
    enabled,
    gcTime: 0,
  });
}
