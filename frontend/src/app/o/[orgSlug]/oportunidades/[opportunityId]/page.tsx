import { notFound } from "next/navigation";

import { OpportunityDetail } from "@/components/deals/opportunity-detail";

// El conversor `<uuid:opportunity_id>` de la API: cualquier otra cosa no es una oportunidad y no se
// pide (tampoco se arma con ella una ruta de la API).
const OPPORTUNITY_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export default async function OpportunityPage({
  params,
}: {
  params: Promise<{ orgSlug: string; opportunityId: string }>;
}) {
  const { opportunityId } = await params;
  if (!OPPORTUNITY_ID.test(opportunityId)) notFound();
  return <OpportunityDetail opportunityId={opportunityId} />;
}
