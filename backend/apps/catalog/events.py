"""Lo que el catálogo avisa a los módulos de encima (module-dependencies §3: `events` es público).

`catalog` está debajo de `inventory` en las capas y no lo importa: para no borrar una variante
con stock (F3-12), el catálogo pregunta con una señal síncrona y quien guarda algo de ella
(`inventory`, que la escucha desde su `AppConfig.ready`) la veta.
"""

from django.dispatch import Signal

variants_retiring = Signal()
"""Se envía dentro de la transacción del borrado, con las variantes ya bloqueadas como las
bloquea un movimiento (`FOR NO KEY UPDATE`), así que ningún movimiento las cambia a la vez.
Argumentos: `using` (el alias del `tenant_scope`) y `variant_ids`. Un receptor que levanta
`VariantsHeld` impide el borrado sin que se escriba nada."""


class VariantsHeld(Exception):
    """Alguna de las variantes tiene existencias o reservas: no se borra; se desactiva."""
