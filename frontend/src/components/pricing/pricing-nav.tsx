import { SectionNav } from "@/components/catalog/catalog-nav";

// Las páginas de precios (F3-41). Los precios de una lista llegan con F3-45.
const PAGES = [{ key: "lists", path: "/precios/listas" }] as const;

export const PricingNav = () => <SectionNav section="prices" pages={PAGES} />;
