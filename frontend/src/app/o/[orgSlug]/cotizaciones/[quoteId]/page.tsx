import { notFound } from "next/navigation";

import { QuoteDetail } from "@/components/quotes/quote-detail";

// El conversor `<uuid:quote_id>` de la API: cualquier otra cosa no es una cotización y no se pide.
const QUOTE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export default async function QuotePage({
  params,
}: {
  params: Promise<{ orgSlug: string; quoteId: string }>;
}) {
  const { quoteId } = await params;
  if (!QUOTE_ID.test(quoteId)) notFound();
  return <QuoteDetail quoteId={quoteId} />;
}
