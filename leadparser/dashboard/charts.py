"""Инлайновый SVG для дашборда — вручную, без библиотек (бриф: "Никаких
библиотек, зависимости проекта трогать нельзя").

Оба графика ниже — один ряд данных каждый (воронка: число откликов по
стадиям; карма: один аккаунт), поэтому легенда не нужна — бриф требует её
только "если серий больше одной". Цвета заданы именами CSS-переменных
(`var(--series-1)` и т.п.), а не хексами: значения переменных объявлены в
dashboard.html.j2 и переопределяются там же для тёмной темы — если бы хекс
палитры попал прямо в атрибут SVG, тёмная тема на график бы не подействовала.
Текст (подписи и значения) — всегда `--text-primary`/`--text-secondary`,
никогда цветом серии (бриф: "текст всегда текстовыми цветами").
"""
from __future__ import annotations

from datetime import date
from xml.sax.saxutils import escape

_SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'
# Оценка ширины одной цифры при font-size 13 / font-weight 700 — с запасом,
# не точная метрика шрифта (в чистом SVG-без-браузера её и не измерить).
_LABEL_CHAR_PX = 9
_LABEL_PAD = 8       # отступ между концом полосы и началом подписи
_LABEL_SAFETY_PX = 6  # запас поверх оценки ширины символа


def _label_reserve(stages: list[tuple[str, int]]) -> float:
    """Сколько места справа зарезервировать под САМУЮ широкую подпись значения.

    Ревью: раньше подпись ставилась в `margin + bar_w + 8` без учёта того,
    влезет ли она после этого в оставшиеся до края 8px — у самой длинной
    полосы это ровно место на одну цифру, и "128" рендерился обрезанным до
    "1". Резервируем по ФАКТИЧЕСКИ самому длинному значению в этом вызове
    (не фиксированной ширине "на все случаи"), чтобы короткие числа по-прежнему
    получали максимум места для самой полосы.
    """
    widest = max((len(str(v)) for _, v in stages), default=1)
    return widest * _LABEL_CHAR_PX + _LABEL_PAD + _LABEL_SAFETY_PX


def funnel_svg(stages: list[tuple[str, int]], width: int = 640) -> str:
    """Горизонтальные полосы с обязательной подписью значения на каждой —
    бриф для воронки не делает подписи выборочными, в отличие от карты кармы.

    Подпись всегда рисуется ПОСЛЕ полосы (никогда поверх цветной заливки —
    так подпись остаётся текстовым цветом, не цветом серии, без вопроса о
    контрасте текста на заливке); её место резервируется в геометрии заранее
    через `_label_reserve`, а не предполагается по факту.
    """
    margin, bar_h, gap, top_pad = 16, 26, 20, 8
    row_h = bar_h + gap
    height = top_pad + len(stages) * row_h
    track_w = max(width - 2 * margin - _label_reserve(stages), 1)
    max_v = max((v for _, v in stages), default=0) or 1

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
             f'aria-label="Воронка откликов" {_SVG_NS}>']
    for frac in (0.25, 0.5, 0.75, 1.0):
        x = margin + track_w * frac
        parts.append(
            f'<line x1="{x:.1f}" y1="{top_pad}" x2="{x:.1f}" '
            f'y2="{height - gap / 2:.1f}" style="stroke:var(--text-secondary)" '
            f'stroke-width="1" opacity="0.2" />'
        )
    for i, (label, value) in enumerate(stages):
        y = top_pad + i * row_h
        bar_y = y + 14
        bar_w = round(track_w * value / max_v) if value else 0
        parts.append(
            f'<text x="{margin}" y="{y + 10}" style="fill:var(--text-secondary)" '
            f'font-size="12">{escape(str(label))}</text>'
        )
        parts.append(
            f'<rect x="{margin}" y="{bar_y}" width="{bar_w}" height="{bar_h}" '
            f'rx="4" style="fill:var(--series-1)" />'
        )
        parts.append(
            f'<text x="{margin + bar_w + _LABEL_PAD}" y="{bar_y + bar_h - 7}" '
            f'style="fill:var(--text-primary)" font-size="13" '
            f'font-weight="700">{value}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def karma_line_svg(
    history: list[tuple[date, int]], today: date, width: int = 640, height: int = 200,
) -> str:
    """Линия от первой засечки до сегодня; подписаны только первая и
    последняя точка (бриф: "выборочно, не на каждой точке"). Ось X растянута
    до `today`, а не только до последней точки: если проверка кармы отстала,
    точка ляжет заметно левее правого края — сама несвежесть видна на глаз.
    """
    if len(history) < 2:
        raise ValueError("нужно минимум 2 точки — при меньшем числе рисовать нечего")

    margin_x, margin_y = 40, 28
    plot_w, plot_h = width - 2 * margin_x, height - 2 * margin_y
    first_date = history[0][0]
    span_days = max((max(today, history[-1][0]) - first_date).days, 1)
    values = [v for _, v in history]
    v_min, v_max = min(values), max(values)
    v_span = (v_max - v_min) or 1

    def x_for(d: date) -> float:
        return margin_x + plot_w * ((d - first_date).days / span_days)

    def y_for(v: int) -> float:
        return margin_y + plot_h * (1 - (v - v_min) / v_span)

    points = [(x_for(d), y_for(v)) for d, v in history]

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
             f'aria-label="Карма по дням" {_SVG_NS}>']
    for frac in (0.0, 0.5, 1.0):
        y = margin_y + plot_h * frac
        parts.append(
            f'<line x1="{margin_x}" y1="{y:.1f}" x2="{margin_x + plot_w}" y2="{y:.1f}" '
            f'style="stroke:var(--text-secondary)" stroke-width="1" opacity="0.2" />'
        )
    path_d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in points)
    parts.append(
        f'<path d="{path_d}" fill="none" style="stroke:var(--series-3)" stroke-width="2" />'
    )
    for x, y in points:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" style="fill:var(--series-3)" />')
    (first_x, first_y), (last_x, last_y) = points[0], points[-1]
    parts.append(
        f'<text x="{first_x:.1f}" y="{first_y - 10:.1f}" style="fill:var(--text-primary)" '
        f'font-size="12" font-weight="700">{history[0][1]}</text>'
    )
    parts.append(
        f'<text x="{last_x:.1f}" y="{last_y - 10:.1f}" style="fill:var(--text-primary)" '
        f'font-size="12" font-weight="700" text-anchor="end">{history[-1][1]}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)
