"""F8-08f (#1023; ADR-034 §7, §8, §9): el Output Guard II. Lo que el modelo escribe fuera de los
placeholders (cifras, descuentos, stock, plazos, términos y enlaces) no sale del servidor: en la
sugerencia cada tramo pasa a `[verificar]`; en el resumen queda marcado. El traspaso devuelve el
texto fijo, nunca el del modelo. Puro, sin base de datos; todos los datos son ficticios."""

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apps.ai_agents import detectors
from apps.ai_agents.detectors import MAX_FINDINGS, Handoff, handoff, mark, substitute, suggestion
from apps.ai_agents.guard import Finding, Rendered, render
from apps.conversations.models import BODY_MAX
from core.text import VERIFY_MARK
from tests.test_ai_output_guard import EVIDENCE, NOW, claim, reply

M = VERIFY_MARK


def plain(text: str, wants_handoff: bool = False) -> Rendered:
    """Una sugerencia sin placeholders: todo es texto del modelo."""
    return Rendered(text, (), (), wants_handoff, None, ())


# (texto del modelo, el tramo que no puede salir, su tipo)
CASES = [
    ("Cuesta S/ 30", "30", "FIGURE"), ("Cuesta S/30.00", "30", "FIGURE"),
    ("Son S/.45 nada más", "45", "FIGURE"), ("Te sale a 120 soles", "120", "FIGURE"),
    ("Unos treinta soles", "treinta", "FIGURE"), ("Son 25 dólares", "dólares", "FIGURE"),
    ("USD 15 por unidad", "USD", "FIGURE"), ("PEN 99", "99", "FIGURE"), ("$ 12", "12", "FIGURE"),
    ("Precio: €20", "20", "FIGURE"), ("Son １２０ soles", "１２０", "FIGURE"),
    ("Son 1\u200b20 soles", "20", "FIGURE"), ("Cuesta S／ 30", "S／", "FIGURE"),
    ("Son doscientos cincuenta", "doscientos", "FIGURE"), ("Vale 3 lucas", "lucas", "FIGURE"),
    ("Cuesta 1249.90", "1249", "FIGURE"), ("Tenemos ½ docena", "½", "FIGURE"),
    ("Viene en Ⅻ piezas", "Ⅻ", "FIGURE"), ("Son 4٣ soles", "٣", "FIGURE"),
    ("Tiene 20% de descuento", "20%", "DISCOUNT"), ("Hoy 15 ％ menos", "％", "DISCOUNT"),
    ("Cinco por ciento menos", "ciento", "DISCOUNT"), ("Lleva un dscto especial", "dscto",
                                                         "DISCOUNT"),
    ("Está en oferta", "oferta", "DISCOUNT"), ("Promo 2x1 en collares", "2x1", "DISCOUNT"),
    ("Antes costaba más", "Antes", "DISCOUNT"), ("Ahora está más barato", "Ahora", "DISCOUNT"),
    ("Tiene una rebaja", "rebaja", "DISCOUNT"), ("Es una promoción", "promoción", "DISCOUNT"),
    ("Aprovecha la liquidación", "liquidación", "DISCOUNT"), ("Ahorras bastante", "Ahorras",
                                                               "DISCOUNT"),
    ("Sí, hay stock", "stock", "AVAILABILITY"), ("Está agotado", "agotado", "AVAILABILITY"),
    ("Quedan pocas", "Quedan", "AVAILABILITY"), ("Últimas unidades", "unidades", "AVAILABILITY"),
    ("Está disponible", "disponible", "AVAILABILITY"), ("Lo reponemos pronto", "reponemos",
                                                         "AVAILABILITY"),
    ("Hay en existencia", "existencia", "AVAILABILITY"), ("Está ＡＧＯＴＡＤＯ", "ＡＧＯＴＡＤＯ",
                                                           "AVAILABILITY"),
    ("Está ago\u200btado", "tado", "AVAILABILITY"), ("Está \u0430gotado", "gotado", "AVAILABILITY"),
    ("Llega mañana", "mañana", "DELIVERY"), ("Te llega en 24 horas", "24", "DELIVERY"),
    ("La entrega es el sábado", "sábado", "DELIVERY"), ("Lo despachamos hoy", "despachamos",
                                                         "DELIVERY"),
    ("Lo enviamos el lunes", "lunes", "DELIVERY"), ("En dos días hábiles", "hábiles", "DELIVERY"),
    ("Delivery en Lima", "Delivery", "DELIVERY"), ("Sin demora", "demora", "DELIVERY"),
    ("Paga en 3 cuotas", "cuotas", "TERMS"), ("Aceptamos contraentrega", "contraentrega",
                                                "TERMS"),
    ("Pago contra entrega", "entrega", "TERMS"), ("Tiene garantía", "garantía", "TERMS"),
    ("Aceptamos devolución", "devolución", "TERMS"), ("Puedes hacer cambio", "cambio", "TERMS"),
    ("El envío es gratis", "gratis", "TERMS"), ("Te hacemos reembolso", "reembolso", "TERMS"),
    ("Paga con Yape", "Yape", "TERMS"), ("Sin costo adicional", "costo", "TERMS"),
    ("Mira https://tienda.example/x", "tienda", "LINK"), ("Entra a www.tienda.pe", "tienda",
                                                          "LINK"),
    ("Escribe a tienda.pe", "tienda.pe", "LINK"), ("Ve a tienda[.]pe", "tienda", "LINK"),
    ("Ve a tienda(punto)pe", "tienda", "LINK"), ("Ve a tienda punto com", "tienda", "LINK"),
    ("Abre bit.ly/abc123", "abc123", "LINK"), ("Escríbenos a ventas@tienda.pe", "ventas", "LINK"),
    ("Abre ｗｗｗ．tienda．pe", "tienda", "LINK"), ("Abre tienda。pe", "tienda", "LINK"),
    ("Abre wa.me/51", "wa.me", "LINK"), ("Abre hxxps://x", "hxxps", "LINK"),
    ("Escríbenos a @tienda", "tienda", "LINK"), ("Mira tienda . pe", "tienda", "LINK"),
    ("Va así: {c1}", "c1", "MARKUP"), ("Va así: ｛｛c1｝｝", "c1", "MARKUP"),
    ("Va así: } y {", "}", "MARKUP"), ("Va así: \u2774c1\u2775", "\u2774", "MARKUP"),
    ("Ya está [verificar]", "verificar", "MARKUP"),
    ("Ya está [vеrificar]", "\u0435", "MARKUP"), ("Ya [veri\u2009ficar]", "\u2009", "MARKUP"),
    ("Ya está ［ＶＥＲＩＦＩＣＡＲ］", "ＶＥＲ", "MARKUP"),
    # #1039: tras una cifra (la clave la pliega a un signo, no a una letra), palabras compuestas
    ("Hay 5disponibles", "disponibles", "AVAILABILITY"), ("Con 10descuento", "descuento",
                                                            "DISCOUNT"),
    ("En 3cuotas", "cuotas", "TERMS"), ("Envío 0costo", "costo", "TERMS"),
    ("Llega en 2dias", "dias", "DELIVERY"), ("Son 30lucas", "lucas", "FIGURE"),
    ("Hoy 20dscto", "dscto", "DISCOUNT"), ("Son quinientos", "quinientos", "FIGURE"),
    ("Son quinientas", "quinientas", "FIGURE"), ("Son treintaicinco", "treintaicinco", "FIGURE"),
    ("Son treintiuno", "treintiuno", "FIGURE"), ("Cuesta cero", "cero", "FIGURE"),
    ("Está ago\u3164tado", "tado", "AVAILABILITY"),
    ("Ve a tienda punto pet", "tienda", "LINK"), ("Ve a tienda . pet", "tienda", "LINK"),
    ("Ve a tienda .dog", "dog", "LINK"), ("Ve a tienda .mascotas", "mascotas", "LINK"),
    ("Ve a tienda dot vet", "vet", "LINK"),
    ("Ve a tienda\u2022pe", "tienda", "LINK"), ("Ve a tienda\u2219pe", "tienda", "LINK"),
    ("Ve a tienda\u22c5pe", "tienda", "LINK"), ("Ve a tienda\u300c.\u300dpe", "tienda", "LINK"),
    ("Antes ~S/ 40~", "~", "DISCOUNT"), ("De S/ 40 \u2192 S/ 30", "\u2192", "DISCOUNT"),
    ("De 40 -> 30", "->", "DISCOUNT"), ("Costaba más", "Costaba", "DISCOUNT"),
    ("Pasó de un precio a otro", "Pasó", "DISCOUNT"), ("Bajó de precio", "Bajó", "DISCOUNT"),
    ("Precio normal", "normal", "DISCOUNT"), ("En vez de eso", "vez", "DISCOUNT"),
    ("Precio de lista", "lista", "DISCOUNT"), ("Era más caro", "Era", "DISCOUNT"),
]  # fmt: skip

# Cómo se vende por chat en Perú (#1039): cada frase deja una marca
CORPUS = [
    ("Tiene descto", "descto"), ("Con dto", "dto"), ("Hoy desc especial", "desc"),
    ("50 OFF", "OFF"), ("Precio especial para ti", "especial"), ("Está más barato", "barato"),
    ("A mitad de precio", "mitad"), ("Te va de regalo", "regalo"), ("Va sin cargo", "cargo"),
    ("Puedes cancelar al recibir", "cancelar"), ("Aceptamos Visa", "Visa"),
    ("Se acabó", "acabó"), ("Ya no hay", "hay"), ("Sí tenemos", "tenemos"),
    ("Te llega al toque", "toque"), ("Ahorita te lo envío", "Ahorita"),
    ("Te lo mandamos ya", "mandamos"), ("Tu pedido está en camino", "camino"),
    ("Tu pedido ya salió", "salió"), ("Pedido confirmado", "confirmado"),
    ("Ya está listo", "listo"),
]  # fmt: skip


@pytest.mark.parametrize(("text", "needle"), CORPUS)
def test_peruvian_sales_wording_is_marked(text: str, needle: str) -> None:
    out = substitute(plain(text))
    assert needle not in out.text.replace(M, "") and out.findings


@pytest.mark.parametrize(("text", "needle", "kind"), CASES)
def test_nothing_the_model_wrote_survives_in_the_suggestion(
    text: str, needle: str, kind: str
) -> None:
    out = substitute(plain(text))
    assert needle not in out.text.replace(M, "") and M in out.text
    assert kind in {found.type for found in out.findings}
    for found in out.findings:  # tipo y posición de su marca; nunca el tramo
        assert out.text[found.start : found.end] == M
    assert set(out.inserted) == {(found.start, found.end) for found in out.findings}


def test_the_table_is_big_enough_and_covers_every_family() -> None:
    assert len(CASES) >= 60
    assert {kind for *_, kind in CASES} == set(detectors.PATTERNS)


@pytest.mark.parametrize(
    "text",
    ["Hola, ¿en qué te puedo ayudar?", "¡Buenos días! Con gusto te ayudo.", "Claro, déjame ver.",
     "Gracias por escribirnos, que tengas buenas tardes."],
)  # fmt: skip
def test_a_suggestion_with_nothing_to_detect_leaves_intact(text: str) -> None:
    rendered = plain(text)
    assert substitute(rendered) == rendered


def test_server_values_are_never_marked_but_the_model_text_around_them_is() -> None:
    """El precio, la promoción y sus condiciones (cifras, `%`, «semana») son del servidor; el
    `{{{c1}}}` deja llaves del modelo y un número igual a un valor de la evidencia también sale."""
    template = "Hola, el {{c1}} cuesta {{c2}} con {{c3}} ({{c4}}). Antes vi 1249.90, mira {{{c2}}}"
    raw = reply(template,
                claim("c1", "PRODUCT_NAME", "tu_v", "name"),
                claim("c2", "PRICE", "tu_p", "prices.0.amount"),
                claim("c3", "PROMOTION", "tu_p", "prices.0.promotion"),
                claim("c4", "PROMOTION_TERMS", "tu_p", "promotions.0.terms"))  # fmt: skip
    rendered = render(raw, EVIDENCE, NOW)
    assert rendered.ok and rendered.findings == ()
    out = substitute(rendered)
    assert out.text == (
        f"Hola, el Collar Rojo cuesta S/ 1,249.90 con Promo Verano (Solo esta semana). {M} vi {M},"
        f" mira {M}S/ 1,249.90{M}"
    )
    assert [found.type for found in out.findings] == ["DISCOUNT", "FIGURE", "MARKUP", "MARKUP"]


def test_the_guard_findings_move_with_the_text() -> None:
    raw = reply("Son S/ 30 y {{c1}}, o {{c9}} hoy",
                claim("c1", "PRICE", "tu_fail", "prices.0.amount"))  # fmt: skip
    rendered = render(raw, EVIDENCE, NOW)
    out = substitute(rendered)
    assert out.text == f"Son {M} y {M}, o {M} {M}"
    kinds = [(found.type, out.text[found.start : found.end]) for found in out.findings]
    assert kinds == [("FIGURE", M), ("EVIDENCE_MISSING", M), ("CLAIM_MISSING", M), ("DELIVERY", M)]
    assert out.problems == rendered.problems and len(out.inserted) == 4


def server(*parts: str) -> Rendered:
    """`parts` alterna valor del servidor y texto del modelo, empezando por un valor."""
    text, inserted = "", []
    for at, part in enumerate(parts):
        if at % 2 == 0:
            inserted.append((len(text), len(text) + len(part)))
        text += part
    return Rendered(text, (), (), False, None, tuple(inserted))


def test_model_text_glued_to_a_server_value_is_marked() -> None:
    glued = substitute(server("Rojo", ".tiendita y ", "Rojo", " . dog"))
    assert glued.text == f"Rojo{M} y Rojo {M}"
    for word in ["gratis", "quinientos", "stock", "cuotas", "dias", "promo", "envio", "agotado",
                 " punto dog"]:  # fmt: skip
        out = substitute(server("Collar Rojo", word))  # la palabra sola, aunque el valor la pegue
        assert out.text == f"Collar Rojo{M}" if word[0] != " " else f"Collar Rojo {M}"
    assert substitute(server("Collar", "es gratis")).text == f"Collares {M}"
    clean = server("disponible", ", ", "Promo Verano", " ", "S/ 1,249.90", " ", "2 kg")
    assert substitute(clean) == clean  # nada detectado o solo blancos entre dos valores
    assert substitute(server("30", ",", "40")).text == f"30{M}40"  # una cifra hecha de dos
    assert substitute(server("Rojo", ".", "tienda")).text == f"Rojo{M}tienda"  # salvo un enlace


P0, P1, REGULAR = "Precio Publico: S/ 1,249.90", "Tienda: US$ 57.50", "precio regular S/ 1,299.90"


@pytest.mark.parametrize(
    "gap", [" y ", ", ", "\n", "\n- ", " / ", " o ", " | ", " \u2022 ", " \u00b7 "]
)
def test_two_prices_with_a_list_separator_stay(gap: str) -> None:
    for left, right in [(P0, P1), ("S/ 40.00", "S/ 30.00 + IGV")]:
        rendered = server(left, gap, right)
        assert substitute(rendered) == rendered


@pytest.mark.parametrize("bullet", ["\u2022", "\u00b7"])
def test_a_bullet_list_is_not_a_link(bullet: str) -> None:
    text = f"Opciones:\n{bullet} Talla M\n{bullet} Talla L\n{bullet} pet shop"
    assert substitute(plain(text)) == plain(text)
    assert M in substitute(plain(f"Ve a tienda{bullet}pe")).text  # pegado sí es un enlace


GAPS = [
    " a ",
    " hasta ",
    " vs ",
    " > ",
    " \u2014 ",
    " \u25b6 ",
    " \u00bb ",
    " \U0001f449 ",
    " \u23e9 ",
    " \u274c ",
    " (tachado) ",
    " mejor ",
    " y te lo dejo en ",
    " y ahora ",
]


@pytest.mark.parametrize("gap", GAPS)
def test_anything_else_between_two_prices_is_a_discount(gap: str) -> None:
    for left, right in [(P0, P1), (REGULAR, P0), ("S/ 40.00", "S/ 30.00")]:
        out = substitute(server(left, gap, right))
        assert out.text == f"{left}{M}{right}"
        assert [f.type for f in out.findings] == ["DISCOUNT"]


def test_the_summary_marks_without_substituting() -> None:
    text = "El cliente pide 2 collares para el lunes; vio el precio en tienda.pe"
    found = mark(text)
    assert [(f.type, text[f.start : f.end]) for f in found] == [
        ("FIGURE", "2"), ("DELIVERY", "lunes"), ("LINK", "tienda.pe")]  # fmt: skip
    assert mark("El cliente saluda y pregunta por un collar.") == ()


def test_too_many_spans_keep_the_findings_bounded() -> None:
    many = " y ".join(["5"] * (MAX_FINDINGS + 20))
    found = mark(many)
    assert len(found) == MAX_FINDINGS and found[-1].end == len(many)
    out = substitute(plain(many))  # la sugerencia no se puede usar: solo la marca
    assert (out.text, out.findings) == (M, (Finding("TOO_LONG", 0, len(M)),))
    long = substitute(plain("x" * (BODY_MAX - 16) + " 1 y 2"))  # cabe, pero no con sus marcas
    assert long.text == M and [p.code for p in long.problems] == ["TOO_LONG"]


@pytest.mark.parametrize(
    ("customer", "wants", "reason"),
    [
        (["Hola", "Quiero hablar con un asesor"], False, "ASKED_FOR_PERSON"),
        (["¿Me pasas con una persona real?"], False, "ASKED_FOR_PERSON"),
        (["Quiero hablar con alguien"], False, "ASKED_FOR_PERSON"),
        (["¿Eres un bot? Pásame con el encargado"], True, "ASKED_FOR_PERSON"),
        (["Quiero un ＡＳＥＳＯＲ"], False, "ASKED_FOR_PERSON"),
        (["Un ase\u200bsor"], False, "ASKED_FOR_PERSON"),
        (["Con un agente"], False, "ASKED_FOR_PERSON"),
        (["Con tu jefe"], False, "ASKED_FOR_PERSON"),
        (["Con un ejecutivo"], False, "ASKED_FOR_PERSON"),
        (["Pásame con alguien"], False, "ASKED_FOR_PERSON"),
        (["Que me atienda alguien"], False, "ASKED_FOR_PERSON"),
        (["Es una ESTAFA, quiero un asesor"], True, "ASKED_FOR_PERSON"),  # el orden de motivos
        (["Voy a poner un reclamo"], True, "UPSET"),
        (["esto es una ESTAFA!!!"], False, "UPSET"),
        (["Voy a poner un reclamo"], False, "UPSET"),
        (["QUIERO MI PEDIDO YA!!!"], False, "UPSET"),  # mayúsculas sostenidas
        (["NO PUEDE SER!!!"], False, "UPSET"),
        (["YA BASTA!!!"], False, "UPSET"),  # con «!!!»
        (["YA BASTA"], False, None),
        (["OK!!!"], False, None),
        (["Mi perro come harto"], False, None),
        (["¿Tienen máquina para cortar pelo?"], False, None),
        (["¿no le molesta?"], False, None),
        (["Bolsas para basura"], False, None),
        (["Quiero el ROYAL CANIN MAXI ADULT"], False, None),
        (["Hola!!!"], False, None),
        (["Hola, quiero el ROYAL CANIN!!!"], False, None),
        (["GRACIAS!!!"], False, None),
        (["QUE LINDO!!!"], False, None),
        (["GRACIAS POR TODO!!!"], False, None),
        (["LLEGÓ PERFECTO!!!"], False, None),
        (["ME ENCANTÓ!!!"], False, None),
        (["SI!!! LO QUIERO"], False, None),
        (["EXCELENTE SERVICIO!!!"], False, None),
        (["QUE GENIAL!!!"], False, None),
        (["ESTOY FELIZ!!!"], False, None),
        (["BUENAZO!!!"], False, None),
        (["AYUDEN!!!"], False, "UPSET"),
        (["AYUDA!!!"], False, None),  # 6 letras sí, 5 no
        (["¿Es de grado humano?"], False, None),
        (["¿Sirve para humanos?"], False, None),
        (["¿Lo puede usar un humano?"], False, None),
        (["Una persona me recomendó esta tienda"], False, None),
        (["¿Es para una persona o para el perro?"], False, None),
        (["¿Para cuántas personas alcanza?"], False, None),
        (["Quiero hablar con un humano"], False, "ASKED_FOR_PERSON"),
        (["Quiero una persona"], False, "ASKED_FOR_PERSON"),
        (["¿Hay un humano de verdad?"], False, "ASKED_FOR_PERSON"),
        (["HOLA BUENAS TARDES"], False, None),
        (["TIENEN STOCK?"], False, None),
        (["PRECIO DEL NEXGARD?"], False, None),
        (["ROYAL CANIN MEDIUM ADULT 15KG"], False, None),
        (["¿Tienen agente antipulgas?"], False, None),
        (["Es el perro de mi jefe"], False, None),
        (["¿Es para consumo humano?"], False, None),
        (["No es de uso humano"], False, None),
        (["Estoy cansado"], False, None),
        (["Estoy cansada de esperar"], False, "UPSET"),
        (["¿Hablo con un humano?"], False, "ASKED_FOR_PERSON"),
        (["¿Tienen collares rojos?"], True, "MODEL"),
        (["¿Tienen collares rojos?"], False, None),
        ([], False, None),
    ],
)
def test_handoff_is_keywords_or_upset_or_the_model(
    customer: list[str], wants: bool, reason: str | None
) -> None:
    assert handoff(customer, wants) == Handoff(reason is not None, reason)


def test_with_handoff_the_suggestion_is_the_fixed_text_never_the_model_text() -> None:
    rendered = plain("Claro, te atiendo yo mismo.", wants_handoff=True)
    out, found = suggestion(rendered, ["Hola"])
    fixed = detectors.handoff_reply()
    assert found == Handoff(True, "MODEL")
    assert (out.text, out.findings, out.problems) == (fixed, (), ())
    assert "atiendo" not in out.text and out.inserted == ((0, len(fixed)),)
    asked, found = suggestion(plain("Te paso el precio: S/ 30"), ["Quiero un asesor"])
    assert (asked.text, found.reason) == (fixed, "ASKED_FOR_PERSON")
    failed = render(reply("Cuesta {{c1}}", claim("c1", "PRICE", "tu_fail", "prices.0.amount"),
                          handoff=True), EVIDENCE, NOW)  # fmt: skip
    assert failed.findings and failed.problems
    dropped, _ = suggestion(failed, [])
    assert (dropped.text, dropped.findings, dropped.problems) == (fixed, (), ())
    kept, found = suggestion(plain("Claro, te ayudo"), ["Hola"])
    assert (kept.text, found) == ("Claro, te ayudo", Handoff(False))


@pytest.mark.parametrize("value", ["", "  ", None, 5, "Te llamamos en 24 horas", "x" * 5000,
                                   f"Revisa {M}", "Escríbenos a tienda.pe", "\u202eHola",
                                   "Ho\u200bla"])  # fmt: skip
def test_the_handoff_text_is_checked_at_startup_and_on_every_use(value: object) -> None:
    with override_settings(AI_HANDOFF_REPLY=value):
        with pytest.raises(ImproperlyConfigured):
            apps.get_app_config("ai_agents").ready()
        with pytest.raises(ImproperlyConfigured):
            suggestion(plain("Hola"), ["Quiero un asesor"])
    apps.get_app_config("ai_agents").ready()
