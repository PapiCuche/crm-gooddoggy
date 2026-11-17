"""Envía al webhook local un mensaje entrante firmado como Meta (F7b-06c, ADR-041): `page`
(Messenger), `instagram` o `whatsapp`, con un `mid` nuevo en cada envío y la hora actual (la
ventana de 24 h se cuenta desde ella). Firma con `DEV_APP_SECRET`, el `META_APP_SECRETS` de ejemplo
del stack de desarrollo (`infra/env/.env.example`); otro con `--secret`. Solo la biblioteca
estándar.

    python -m tests.meta_sim.send_webhook instagram "Hola" --url http://localhost:8080/webhooks/meta/
"""

import argparse
import dataclasses
import time
import urllib.error
import urllib.request

from tests.meta_sim import social, webhooks

# Ficticio y evidente: letras y dígitos, como exige `META_APP_SECRETS` (`env.check_meta`).
DEV_APP_SECRET = "metasimdevappsecretnotreal0000"  # noqa: S105 — dato ficticio de desarrollo
CHANNELS = {"page": social.MESSENGER, "instagram": social.INSTAGRAM}


def delivery(kind: str, text: str, customer: str | None = None) -> webhooks.Delivery:
    """El cuerpo y las cabeceras **sin firmar**: un `mid` (o `wamid`) único y la hora actual."""
    now, unique = time.time(), time.time_ns()
    if kind == "whatsapp":
        message = webhooks.text_message(
            text, wamid=f"wamid.SIMDEV{unique}", sender=customer or webhooks.CUSTOMER, at=int(now)
        )
        body = webhooks.encode(webhooks.envelope(webhooks.value(messages=[message])))
        return webhooks.deliver(body, secret=None)
    channel = CHANNELS[kind]
    if customer:
        channel = dataclasses.replace(channel, customer=customer)
    event = social.text(channel, text, mid=f"m_SIMDEV{unique}", at=int(now * 1000))
    return webhooks.deliver(webhooks.encode(social.envelope(channel, event)), secret=None)


def send(url: str, kind: str, text: str, secret: str, customer: str | None = None) -> int:
    found = delivery(kind, text, customer)
    headers = {**found.headers, webhooks.SIGNATURE_HEADER: webhooks.sign(found.body, secret)}
    if not url.startswith(("http://", "https://")):
        raise ValueError("--url: http o https")
    request = urllib.request.Request(url, data=found.body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:  # noqa: S310 — esquema comprobado
            return int(reply.status)
    except urllib.error.HTTPError as error:
        return error.code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Webhook firmado de Meta contra el stack local")
    parser.add_argument("kind", choices=[*CHANNELS, "whatsapp"])
    parser.add_argument("text", nargs="?", default="Hola desde el simulador")
    parser.add_argument("--url", default="http://localhost:8080/webhooks/meta/")
    parser.add_argument("--secret", default=DEV_APP_SECRET)
    parser.add_argument("--customer", help="PSID, IGSID o wa_id (por omisión, el del simulador)")
    args = parser.parse_args()
    status = send(args.url, args.kind, args.text, args.secret, args.customer)
    print(f"{args.kind}: HTTP {status}")  # noqa: T201
    raise SystemExit(0 if status == 200 else 1)
