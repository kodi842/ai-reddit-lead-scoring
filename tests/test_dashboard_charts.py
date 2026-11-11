# tests/test_dashboard_charts.py — инлайновый SVG дашборда (без библиотек,
# см. leadparser/dashboard/charts.py и правила визуализации в брифе).
from datetime import date
from xml.etree import ElementTree as ET

import pytest

from leadparser.dashboard.charts import funnel_svg, karma_line_svg

# Палитра из брифа: сами хексы не должны попадать в SVG буквально — иначе
# тёмная тема (переопределение переменных в CSS) на график не подействует.
PALETTE_HEXES = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#fcfcfb", "#0b0b0b", "#52514e",
    "#3987e5", "#d95926", "#199e70", "#c98500", "#1a1a19", "#ffffff", "#c3c2b7",
)


def _parse(svg: str) -> ET.Element:
    """Годный XML — то же самое, что честный HTML-парсер сказал бы браузеру:
    несбалансированный тег или неэкранированный `&` не пройдёт мимо этой
    проверки, как прошёл бы мимо простого grep по подстрокам."""
    return ET.fromstring(svg)


# --- funnel_svg ---

FUNNEL_STAGES = [
    ("Отправлено", 12), ("Ответили", 5), ("Переговоры", 3),
    ("Заказов", 2), ("Оплачено", 1),
]


def test_funnel_svg_is_well_formed_xml():
    _parse(funnel_svg(FUNNEL_STAGES))


def test_funnel_svg_contains_every_label_and_value():
    svg = funnel_svg(FUNNEL_STAGES)
    for label, value in FUNNEL_STAGES:
        assert f">{label}<" in svg
        assert f">{value}<" in svg


def test_funnel_svg_value_labels_never_overflow_the_viewbox():
    """Ревью: `margin + bar_w + 8` оставлял у самой широкой полосы ровно 8px
    до правого края viewBox — места на одну цифру. Двузначное "12" ещё как-то
    помещалось, но "128" (владелец уже сегодня растит отклики к ~30 в неделю,
    а тест на всякий случай берёт с запасом) рендерился обрезанным до "1".
    Подпись обязана умещаться целиком при любом количестве цифр — геометрия
    должна резервировать место заранее, а не полагаться на то, что текст
    влезет."""
    stage_sets = [
        FUNNEL_STAGES,  # исходные двузначные значения — не должно было ломаться и раньше
        [("Отправлено", 128), ("Ответили", 40), ("Переговоры", 12),
         ("Заказов", 3), ("Оплачено", 1)],
    ]
    for stages in stage_sets:
        svg = funnel_svg(stages)
        root = _parse(svg)
        width = float(root.get("viewBox").split()[2])
        texts = root.findall(".//{http://www.w3.org/2000/svg}text")
        for _label, value in stages:
            value_label = next(t for t in texts if t.text == str(value))
            label_x = float(value_label.get("x"))
            # Заведомо шире реального рендера жирной 13px-цифры — если оценка
            # с запасом всё равно укладывается в границу, в браузере тем более.
            conservative_char_px = 10
            estimated_right_edge = label_x + len(str(value)) * conservative_char_px
            assert estimated_right_edge <= width - 16, (
                f"значение {value!r}: оценка правого края подписи "
                f"{estimated_right_edge} вылезает за границу {width - 16} "
                f"(ширина графика {width})"
            )


def test_funnel_svg_bar_widths_reflect_relative_values():
    svg = funnel_svg([("A", 10), ("B", 2)])
    rects = _parse(svg).findall(".//{http://www.w3.org/2000/svg}rect")
    widths = [float(r.get("width")) for r in rects]
    assert widths[0] > widths[1] > 0


def test_funnel_svg_handles_all_zero_values_without_crashing():
    svg = funnel_svg([("Отправлено", 0), ("Ответили", 0)])
    _parse(svg)
    assert ">0<" in svg


def test_funnel_svg_uses_css_variables_not_literal_palette_hexes():
    svg = funnel_svg(FUNNEL_STAGES)
    assert "var(--series-1)" in svg
    for hex_code in PALETTE_HEXES:
        assert hex_code not in svg


def test_funnel_svg_escapes_labels():
    svg = funnel_svg([("<script>alert(1)</script>", 5)])
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


# --- karma_line_svg ---

KARMA_HISTORY = [
    (date(2026, 9, 10), 100),
    (date(2026, 9, 15), 108),
    (date(2026, 9, 20), 130),
]


def test_karma_line_svg_requires_at_least_two_points():
    with pytest.raises(ValueError):
        karma_line_svg([(date(2026, 9, 10), 100)], today=date(2026, 9, 24))


def test_karma_line_svg_is_well_formed_xml():
    _parse(karma_line_svg(KARMA_HISTORY, today=date(2026, 9, 24)))


def test_karma_line_svg_labels_only_first_and_last_point():
    svg = karma_line_svg(KARMA_HISTORY, today=date(2026, 9, 24))
    texts = _parse(svg).findall(".//{http://www.w3.org/2000/svg}text")
    values = [t.text for t in texts]
    assert values == ["100", "130"]


def test_karma_line_svg_draws_one_marker_per_point_with_8px_diameter():
    svg = karma_line_svg(KARMA_HISTORY, today=date(2026, 9, 24))
    circles = _parse(svg).findall(".//{http://www.w3.org/2000/svg}circle")
    assert len(circles) == len(KARMA_HISTORY)
    assert all(float(c.get("r")) >= 4 for c in circles)  # диаметр >= 8px


def test_karma_line_svg_uses_2px_stroke_and_no_literal_palette_hexes():
    svg = karma_line_svg(KARMA_HISTORY, today=date(2026, 9, 24))
    path = _parse(svg).find(".//{http://www.w3.org/2000/svg}path")
    assert path.get("stroke-width") == "2"
    for hex_code in PALETTE_HEXES:
        assert hex_code not in svg


def test_karma_line_svg_domain_extends_to_today_not_just_the_last_point():
    """"От первой засечки до сегодня" — если последняя проверка кармы была
    несколько дней назад, точка должна лечь заметно левее правого края, а не
    ровно на нём: иначе несвежесть данных выглядела бы как их отсутствие."""
    svg_fresh = karma_line_svg(KARMA_HISTORY, today=date(2026, 9, 20))  # = последняя точка
    svg_stale = karma_line_svg(KARMA_HISTORY, today=date(2026, 9, 30))  # на 10 дней позже
    last_x_fresh = _parse(svg_fresh).findall(".//{http://www.w3.org/2000/svg}circle")[-1].get("cx")
    last_x_stale = _parse(svg_stale).findall(".//{http://www.w3.org/2000/svg}circle")[-1].get("cx")
    assert float(last_x_stale) < float(last_x_fresh)


def test_karma_line_svg_handles_flat_history_without_crashing():
    flat = [(date(2026, 9, 10), 100), (date(2026, 9, 20), 100)]
    svg = karma_line_svg(flat, today=date(2026, 9, 24))
    _parse(svg)
