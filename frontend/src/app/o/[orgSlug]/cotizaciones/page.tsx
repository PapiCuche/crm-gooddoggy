import { QuotesList } from "@/components/quotes/quotes-list";

// El UUID de un cliente que viene en la dirección (`?cliente=`) filtra la lista; cualquier otra
// cosa no es un filtro y se ignora.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export default async function QuotesPage({
  searchParams,
}: {
  searchParams: Promise<{ cliente?: string | string[] }>;
}) {
  const { cliente } = await searchParams;
  return (
    <QuotesList
      contactId={typeof cliente === "string" && UUID.test(cliente) ? cliente : undefined}
    />
  );
}
