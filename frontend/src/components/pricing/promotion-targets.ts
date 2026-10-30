import { useQuery } from "@tanstack/react-query";

import { everyPage } from "@/components/catalog/categories-tree";
import {
  brandsList,
  categoriesList,
  getBrandsListQueryKey,
  productsList,
  variantsList,
} from "@/lib/api/client";
import type { PromotionItem } from "@/lib/api/model";

export const KINDS = ["variant_id", "product_id", "category_id", "brand_id"] as const;
// Tope de peticiones de variantes: una variante solo se lee por su producto.
const VARIANT_READS = 25;

// Los nombres de los objetivos (la API solo da ids), de los listados del catálogo.
export function useTargetNames(slug: string, items: readonly PromotionItem[]) {
  const wanted = new Set(items.flatMap((item) => KINDS.map((kind) => item[kind]).filter(Boolean)));
  const query = useQuery({
    queryKey: [...getBrandsListQueryKey(slug), "promotion-targets", [...wanted].sort()],
    queryFn: async ({ signal }) => {
      const names = new Map<string, string>();
      const uses = (kind: (typeof KINDS)[number]) => items.some((item) => item[kind]);
      const [brands, categories, products] = await Promise.allSettled([
        uses("brand_id") ? everyPage((p) => brandsList(slug, p, { signal })) : [],
        uses("category_id") ? everyPage((p) => categoriesList(slug, p, { signal })) : [],
        uses("product_id") || uses("variant_id")
          ? everyPage((p) => productsList(slug, p, { signal }))
          : [],
      ]);
      for (const one of [brands, categories, products]) {
        if (one.status === "fulfilled") one.value.forEach((row) => names.set(row.id, row.name));
      }
      if (uses("variant_id") && products.status === "fulfilled") {
        const missing = () => items.some((i) => i.variant_id && !names.has(i.variant_id));
        let reads = 0;
        for (const product of products.value) {
          if (!missing() || reads++ >= VARIANT_READS) break;
          try {
            const page = await variantsList(slug, product.id, { limit: 200 }, { signal });
            page.results.forEach((v) => names.set(v.id, `${v.name} (${v.sku})`));
          } catch {}
        }
      }
      return { names, failed: [...wanted].some((id) => !names.has(id as string)) };
    },
    enabled: wanted.size > 0,
    gcTime: 0,
    retry: false,
  });
  return { names: query.data?.names, failed: query.isError || !!query.data?.failed };
}
