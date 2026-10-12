"""
Contexte du tableau de bord, dans l'ordre du cahier des charges §6.5 :

projet étudié → décision → alimentation à utiliser → explication du
bilan et de la décision → comparaison avant/après → preuves, sources,
hypothèses et limites.
"""

from __future__ import annotations

import math

from .sizing import (
    STATUS_LABELS,
    STATUS_NEEDS_CHANGE,
    lifecycle_estimate,
    measurement_comparison,
)


CHART_WIDTH = 760
CHART_HEIGHT = 230
CHART_LEFT = 64
CHART_RIGHT = 12
CHART_TOP = 14
CHART_BOTTOM = 34
MIN_PLATEAU_PX = 4


def _format_duration(ms):
    if ms >= 3_600_000:
        return f'{ms / 3_600_000:.1f} h'.replace('.', ',')
    if ms >= 60_000:
        return f'{ms / 60_000:.1f} min'.replace('.', ',')
    if ms >= 1000:
        return f'{ms / 1000:.1f} s'.replace('.', ',')
    return f'{ms:.0f} ms'


def _format_current(ma):
    if ma >= 1:
        return f'{ma:g} mA'.replace('.', ',')
    return f'{ma * 1000:g} µA'.replace('.', ',')


def plateau_chart(plateaus):
    """Courbe en plateaux (SVG) : temps en abscisse, courant en log."""

    plateaus = [item for item in plateaus or [] if item['end_ms'] > item['start_ms']]

    if not plateaus:
        return None

    total_ms = plateaus[-1]['end_ms']
    plot_w = CHART_WIDTH - CHART_LEFT - CHART_RIGHT
    plot_h = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM

    # Largeurs proportionnelles, avec un minimum pour les phases brèves.
    widths = [
        (item['end_ms'] - item['start_ms']) / total_ms * plot_w
        for item in plateaus
    ]
    short = [width < MIN_PLATEAU_PX for width in widths]
    compressed = any(short)

    if compressed:
        fixed = MIN_PLATEAU_PX * sum(short)
        flexible = sum(width for width, is_short in zip(widths, short) if not is_short)
        scale = (plot_w - fixed) / flexible if flexible else 0
        widths = [
            MIN_PLATEAU_PX if is_short else width * scale
            for width, is_short in zip(widths, short)
        ]

    currents = [item['current_ma'] for item in plateaus]
    positive = [value for value in currents if value > 0]
    high = max(positive) if positive else 1
    low = min(positive) if positive else high

    # Une demi-décade de marge pour que la veille ne se confonde pas
    # avec l'axe.
    top_decade = math.ceil(math.log10(high))
    bottom_decade = math.floor(math.log10(low) - 0.5)
    if top_decade == bottom_decade:
        bottom_decade -= 1

    def y_of(current):
        if current <= 0:
            return CHART_TOP + plot_h
        ratio = (math.log10(current) - bottom_decade) / (top_decade - bottom_decade)
        return CHART_TOP + plot_h * (1 - ratio)

    # Coordonnées en chaînes : les gabarits localisent les flottants
    # (virgule décimale en français), ce qui casserait le SVG.
    def svg(value):
        return f'{value:.2f}'

    x = CHART_LEFT
    points = []
    segments = []

    for item, width in zip(plateaus, widths):
        y = y_of(item['current_ma'])
        points += [(x, y), (x + width, y)]
        segments.append({
            'x': svg(x),
            'width': svg(width),
            'y': svg(y),
            'height': svg(CHART_TOP + plot_h - y),
            'label': (
                f"{_format_current(item['current_ma'])} pendant "
                f"{_format_duration(item['end_ms'] - item['start_ms'])}"
            ),
        })
        x += width

    path = 'M ' + ' L '.join(f'{px:.2f} {py:.2f}' for px, py in points)

    y_ticks = [
        {'y': svg(y_of(10 ** decade)), 'label': _format_current(10 ** decade)}
        for decade in range(bottom_decade, top_decade + 1)
    ]

    bottom = CHART_TOP + plot_h

    return {
        'width': CHART_WIDTH,
        'height': CHART_HEIGHT,
        'left': CHART_LEFT,
        'label_x': CHART_LEFT - 8,
        'right': CHART_WIDTH - CHART_RIGHT,
        'top': CHART_TOP,
        'bottom': bottom,
        'label_y': bottom + 20,
        'path': path,
        'segments': segments,
        'y_ticks': y_ticks,
        'x_end_label': _format_duration(total_ms),
        'compressed': compressed,
    }


def monthly_chart(monthly):
    """
    Barres simples : production solaire prévue par jour, mois par mois,
    face à la consommation journalière (ligne horizontale).
    """

    if not monthly:
        return None

    width, height = 760, 240
    left, right, top, bottom = 56, 12, 16, 200

    values = [item['production_wh'] for item in monthly]
    consumption = monthly[0]['consumption_wh']

    # Graduations « rondes » (1, 2, 2,5 ou 5 × 10^n), environ 4 intervalles.
    raw = max(values + [consumption]) / 4 or 1
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)
    peak = step * math.ceil(max(values + [consumption]) / step)
    if peak <= 0:
        peak = step

    def y_of(value):
        return bottom - (bottom - top) * value / peak

    slot = (width - left - right) / len(monthly)
    bar = slot * 0.62

    def svg(value):
        return f'{value:.2f}'

    def wh(value):
        return f'{value:.3f}'.replace('.', ',') + ' Wh'

    bars = []
    for index, item in enumerate(monthly):
        y = y_of(item['production_wh'])
        x = left + index * slot + (slot - bar) / 2
        bars.append({
            'x': svg(x),
            'y': svg(y),
            'width': svg(bar),
            'height': svg(bottom - y),
            'label_x': svg(x + bar / 2),
            'month': item['month'],
            'short': item['production_wh'] < consumption,
            'title': f"{item['month']} : {wh(item['production_wh'])} produits par jour",
        })

    decimals = max(0, -math.floor(math.log10(step)) + (1 if step / power == 2.5 else 0))
    ticks = []
    value = 0.0
    while value <= peak + step / 1000:
        label = f'{value:.{decimals}f}'.replace('.', ',')
        ticks.append({'y': svg(y_of(value)), 'label': label})
        value += step

    worst = min(monthly, key=lambda item: item['production_wh'])

    return {
        'width': width,
        'height': height,
        'left': left,
        'right': width - right,
        'bottom': bottom,
        'label_y': bottom + 20,
        'bars': bars,
        'ticks': ticks,
        'consumption_y': svg(y_of(consumption)),
        'consumption': wh(consumption),
        'worst_month': worst['month'],
        'worst_ratio': worst['production_wh'] / consumption if consumption else None,
        'all_covered': all(not item['short'] for item in bars),
    }


def _element(section):
    """Statut lisible d'un élément batterie / panneau."""

    if not section:
        return None

    return {
        **section,
        'status_label': STATUS_LABELS.get(section['status'], section['status']),
        'needs_change': section['status'] in STATUS_NEEDS_CHANGE,
    }


def build_report_context(project, result):
    context = {'project': project, 'result': result}

    report = result.full_report if result else None

    if not isinstance(report, dict) or report.get('version') != 2:
        return context

    sizing = report['sizing']
    energy = report.get('energy')
    ai = report.get('ai') or {}

    consumption_ai = ai.get('consumption') or {}
    solar_ai = ai.get('solar') or {}

    removed = (
        consumption_ai.get('removed_sentences', 0)
        + solar_ai.get('removed_sentences', 0)
    )

    cited = set(consumption_ai.get('sources', [])) | set(solar_ai.get('sources', []))

    context.update({
        'report': report,
        'decision': sizing['decision'],
        'energy': energy,
        'sun': report.get('sun'),
        'battery': _element(sizing['battery']),
        'solar': _element(sizing['solar']),
        'hardware': sizing['hardware'],
        'electronics': sizing['electronics'],
        'comparison': sizing['comparison'],
        'hypotheses': sizing['hypotheses'],
        'limits': sizing['limits'],
        'sources': report.get('sources', []),
        'documents': [
            {**doc, 'cited': doc['id'] in cited}
            for doc in report.get('documents', [])
        ],
        'chart': plateau_chart((energy or {}).get('plateaus')),
        'year_chart': monthly_chart(sizing.get('monthly')),
        'measurement': measurement_comparison(
            energy, sizing['battery'], sizing['solar'],
            sizing.get('monthly'), project.measured_current_ma,
        ),
        # Les rapports antérieurs à l'estimation de durée de vie la
        # recalculent à partir des données déjà enregistrées.
        'lifecycle': (
            sizing['lifecycle'] if 'lifecycle' in sizing
            else lifecycle_estimate(
                energy, sizing['battery'], sizing['solar'],
                sizing.get('monthly'),
            )
        ),
        'actions': sizing.get('actions', []),
        'modes': (energy or {}).get('states', []),
        'consumption_ai': consumption_ai,
        'solar_ai': solar_ai,
        'ai_error': ai.get('error'),
        'ai_model': ai.get('model'),
        'ai_removed': removed,
        'solar_disabled': sizing['solar'] is None and energy is not None,
    })

    return context
