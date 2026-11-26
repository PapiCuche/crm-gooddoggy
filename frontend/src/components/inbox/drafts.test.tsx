import { act, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";

import { Drafts, useDraftsValue } from "./drafts";

describe("el almacén de borradores (UX-INBOX-04; el recorrido, en `composer.test.tsx`)", () => {
  it("separa conversación y modo; un envío en curso se queda; otra organización no escribe", () => {
    const acme = new Drafts("o1");
    const [reply, note] = [acme.key("1", "reply"), acme.key("1", "note")];
    acme.update(reply, () => ({ text: "Hola" }));
    acme.update(note, () => ({ text: "Nota" }));
    const texts = () => [reply, note, acme.key("2", "reply")].map((key) => acme.get(key).text);
    expect(texts()).toEqual(["Hola", "Nota", ""]);
    acme.update(reply, () => ({ text: "", sending: true })); // lo enviado ya salió del cuadro
    expect(acme.get(reply).sending).toBe(true);
    acme.update(reply, () => ({ sending: false }));
    expect(acme.get(reply)).toBe(acme.get("o1/9/reply")); // vacío: no ocupa lugar
    const beta = new Drafts("o2"); // los mismos ids de conversación
    beta.update(note, () => ({ text: "de Acme" }));
    expect([beta.get(note).text, beta.key("1", "note") === note]).toEqual(["", false]);
  });

  it("al desmontarse (cerrar sesión, otra organización) se vacía, avisa y no acepta nada más", () => {
    let drafts!: Drafts;
    const Probe = () => ((drafts = useDraftsValue((all) => all)), null);
    const tenant = { user: { id: "u1" }, organization: { id: "o1" } } as SelfContext;
    const view = render(<TenantProvider value={tenant}>{<Probe />}</TenantProvider>);
    const [key, heard] = [drafts.key("1", "reply"), vi.fn()];
    drafts.subscribe(heard);
    act(() => drafts.update(key, () => ({ text: "Hola", sending: true })));
    act(() => drafts.update(drafts.key("2", "note"), () => ({ text: "Nota" })));
    expect([drafts.has("1"), drafts.has("2")]).toEqual([false, true]); // lo que sale no marca
    view.unmount();
    expect(drafts.get(key)).toEqual({ text: "", attempt: null, sending: false });
    drafts.update(key, () => ({ text: "tarde" })); // un envío que termina después
    expect([drafts.get(key).text, heard.mock.calls.length, drafts.has("2")]).toEqual([
      "",
      3,
      false,
    ]);
  });
});
