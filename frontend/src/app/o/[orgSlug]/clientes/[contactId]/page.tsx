import { notFound } from "next/navigation";

import { ContactCard } from "@/components/contacts/contact-card";

// El conversor `<uuid:contact_id>` de la API: cualquier otra cosa no es un contacto y no se pide
// (tampoco se arma con ella una ruta de la API).
const CONTACT_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export default async function ContactPage({
  params,
}: {
  params: Promise<{ orgSlug: string; contactId: string }>;
}) {
  const { contactId } = await params;
  if (!CONTACT_ID.test(contactId)) notFound();
  return <ContactCard contactId={contactId} />;
}
