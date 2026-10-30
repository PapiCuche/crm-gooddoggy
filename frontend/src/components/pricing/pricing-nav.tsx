import { SectionNav } from "@/components/catalog/catalog-nav";

// Las páginas de precios: las listas (F3-41), los precios de una lista (F3-45) y las
// promociones (F3-48f, que leen con `prices.view`: sin él, la pestaña no se ofrece).
const PAGES = [
  { key: "lists", path: "/precios/listas" },
  { key: "prices", path: "/precios" },
  { key: "promotions", path: "/precios/promociones", permission: "prices.view" },
] as const;

export const PricingNav = () => <SectionNav section="prices" pages={PAGES} />;
