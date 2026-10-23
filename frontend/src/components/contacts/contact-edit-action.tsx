"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Choice } from "@/components/catalog/product-create";
import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { useContactsUpdate } from "@/lib/api/client";
import {
  type Contact,
  ContactDocumentTypeEnum,
  type PatchedContactUpdateRequest,
} from "@/lib/api/model";
import { cn } from "@/lib/utils";

// Los del alta, con sus límites. El nombre para mostrar ya existe: no se puede dejar vacío.
const FIELDS = [
  { name: "first_name", max: 100 },
  { name: "last_name", max: 100 },
  { name: "display_name", max: 200 },
  { name: "primary_phone_e164", max: 32, inputMode: "tel", literal: true, hinted: true },
  { name: "primary_email", max: 254, inputMode: "email", literal: true },
  { name: "document_number", max: 32, literal: true, hinted: true },
] as const;
type Name = (typeof FIELDS)[number]["name"];

// Editar un cliente (F4-05) con `PATCH …/contacts/{id}/` (F4-03): viaja solo lo que cambió
// respecto a lo que había al abrir, con la `version` leída. Si otro lo cambió antes (409), no se
// pisa: la lista se vuelve a pedir y el formulario se recarga con lo nuevo. Un 404 es que ya no
// está en su alcance de edición o que se borró: lo explica la lista.
export function ContactEditAction({
  slug,
  contact,
  listKey,
  onAsk,
  onDone,
  onStale,
}: {
  slug: string;
  contact: Contact;
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el aviso anterior de la lista ya no aplica
  onDone: (text: string) => void;
  onStale: (name: string, here: boolean) => void; // `here`: el foco seguía en esta fila
}) {
  const t = useTranslations("contacts.edit");
  const fields = useTranslations("contacts.create"); // los campos son los del alta
  const queryClient = useQueryClient();
  // Lo que había al abrir (`null`: cerrado): con eso se compara, no con la fila que cambie debajo.
  const [from, setFrom] = useState<Contact | null>(null);
  const [round, setRound] = useState(0); // otra vuelta del formulario: los valores recargados
  const [documentType, setDocumentType] = useState<ContactDocumentTypeEnum | "">("");
  const [conflict, setConflict] = useState<"reloaded" | "failed">("reloaded");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const here = () => {
    const active = document.activeElement;
    return active === document.body || !!root.current?.closest("li")?.contains(active);
  };
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const update = useContactsUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved) => {
        // Sigue «Guardando…» hasta que la lista vuelve. Si releerla falla, se dice.
        await queryClient.invalidateQueries({ queryKey: listKey });
        const stale = queryClient.getQueryState(listKey)?.status === "error";
        setFrom(null);
        onDone(t(stale ? "doneStale" : "done", { name: saved.display_name }));
      },
      // Sigue «Guardando…» mientras vuelve la lista: el aviso llega con el formulario recargado.
      onError: async (error) => {
        if (error.code === "CONTACT_VERSION_CONFLICT") {
          await queryClient.invalidateQueries({ queryKey: listKey });
          if (queryClient.getQueryState(listKey)?.status === "error") {
            return setConflict("failed"); // sin lo nuevo, el formulario sigue igual: no se pisa
          }
          const fresh = queryClient
            .getQueryData<InfiniteData<{ results: Contact[] }>>(listKey)
            ?.pages.flatMap((page) => page.results)
            .find((row) => row.id === contact.id);
          if (fresh) {
            setConflict("reloaded");
            return load(fresh);
          }
        } else if (error.status !== 404) return;
        // Un 404, o un 409 de uno que ya no está entre los que ve: lo explica la lista.
        const focused = here();
        setFrom(null);
        onStale(contact.display_name, focused);
      },
    },
  });

  function load(row: Contact) {
    setFrom(row);
    setDocumentType(row.document_type ?? "");
    setRound((now) => now + 1);
  }

  const open = from !== null;
  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Editar», salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    update.reset();
    if (next) {
      onAsk();
      load(contact);
    } else setFrom(null);
  }
  const title = t("title", { name: from?.display_name ?? contact.display_name });

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-3", open && "w-full")}
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {from ? (
        <FieldsForm<Name>
          key={round}
          title={title}
          heading={<h2 className="font-medium wrap-anywhere">{title}</h2>}
          className="flex w-full max-w-xl flex-col gap-4"
          fields={FIELDS.map((field) => ({
            ...field,
            defaultValue: from[field.name] ?? "",
            label: field.name === "display_name" ? t("display_name") : fields(field.name),
            invalid:
              field.name === "document_number" && !documentType
                ? fields("documentTypeMissing")
                : field.name === "display_name"
                  ? t("display_nameInvalid")
                  : fields(`${field.name}Invalid`),
            // Un número sin su tipo, o un tipo sin número, no se envían: la API los rechazaría.
            valid: field.name === "document_number" ? () => !!documentType : undefined,
            required:
              field.name === "display_name"
                ? t("display_nameRequired")
                : field.name === "document_number" && documentType
                  ? fields("documentNumberMissing")
                  : undefined,
            hint: "hinted" in field ? fields(`${field.name}Hint`) : undefined,
          }))}
          note={
            <Choice
              label={fields("document_type")}
              value={documentType}
              busy={update.isPending}
              onChange={(value) => setDocumentType(value as ContactDocumentTypeEnum | "")}
              options={[
                ["", fields("noDocument")],
                ...Object.values(ContactDocumentTypeEnum).map((code): [string, string] => [
                  code,
                  fields(`documentType.${code}`),
                ]),
              ]}
            />
          }
          write={update}
          send={(values) => {
            // Solo lo que cambió: no pisa lo que otro cambió en lo demás. Vacío borra el dato.
            const data: Partial<Record<Name | "document_type", string | null>> = {};
            for (const { name } of FIELDS) {
              if (values[name] === (from[name] ?? "")) continue;
              // Vacío, el nombre y el apellido son ""; el teléfono, el correo y el número, `null`.
              data[name] = values[name] || (name.endsWith("_name") ? "" : null);
            }
            if (documentType !== (from.document_type ?? "")) {
              data.document_type = documentType || null;
              data.document_number = values.document_number || null; // juntos: el error, al número
            }
            if (Object.keys(data).length === 0) return show(false); // nada que cambiar
            update.mutate({
              orgSlug: slug,
              contactId: from.id,
              data: { ...data, version: from.version } as PatchedContactUpdateRequest,
            });
          }}
          taken={(error) => {
            if (error.code === "CONTACT_VERSION_CONFLICT")
              return { text: t(`conflict_${conflict}`) };
            // El tipo sin número (el guardado faltaba): como en el alta, junto al número.
            return error.fields && Object.hasOwn(error.fields, "document_type")
              ? { field: "document_number", text: fields("documentNumberMissing") }
              : null;
          }}
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: fields("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("triggerLabel", { name: contact.display_name })}
          onClick={() => show(true)}
        >
          {t("trigger")}
        </Button>
      )}
    </div>
  );
}
