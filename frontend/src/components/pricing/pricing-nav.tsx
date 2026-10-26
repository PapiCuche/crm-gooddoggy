import { SectionNav } from "@/components/catalog/catalog-nav";

// Las páginas de precios: las listas (F3-41) y los precios de una lista (F3-45).
const PAGES = [
  { key: "lists", path: "/precios/listas" },
  { key: "prices", path: "/precios" },
] as const;

export const PricingNav = () => <SectionNav section="prices" pages={PAGES} />;
