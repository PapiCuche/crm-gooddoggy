// El texto de `messages` para `code` (`users.manage` es `messages.users.manage`), si existe y es
// un texto. El código viene de la API: se busca por propiedades propias, paso a paso, y no como
// una ruta de mensajes, que resolvería también `users` (un objeto) o `users.constructor.name`.
export function text(messages: unknown, code: string): string | null {
  let node = messages;
  for (const part of code.split(".")) {
    if (typeof node !== "object" || node === null || !Object.hasOwn(node, part)) return null;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : null;
}
