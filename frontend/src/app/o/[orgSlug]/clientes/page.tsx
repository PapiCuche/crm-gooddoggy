import { ContactsList } from "@/components/contacts/contacts-list";
import { ContactsNav } from "@/components/contacts/tag-catalog";

export default function ContactsPage() {
  return (
    <>
      <ContactsNav />
      <ContactsList />
    </>
  );
}
