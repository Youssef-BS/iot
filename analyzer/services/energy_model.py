"""
Modèle énergétique déterministe de référence.

Ce module est la source de vérité pour les valeurs énergétiques :
il est utilisé par le payload n8n, par le calculateur local de secours
et par le tableau de bord. Les valeurs proviennent uniquement des modes
saisis (courant, durée, répétitions) — aucune estimation par IA.

Hypothèse de cycle (identique pour tous les chemins) :
- les composants fonctionnent en parallèle ;
- le cycle système dure autant que la séquence la plus longue d'un
  composant ;
- chaque composant exécute sa séquence une fois par cycle système.
"""

from __future__ import annotations

MS_PER_DAY = 86_400_000
MS_PER_HOUR = 3_600_000
DAYS_PER_MONTH = 365.25 / 12
DAYS_PER_YEAR = 365.25
DEFAULT_VOLTAGE_V = 3.3


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    return value if value > 0 else None


def compute_energy_profile(project):
    """
    Calcule le profil énergétique exact du projet à partir de ses modes.

    Retourne None si aucun mode exploitable n'est saisi.
    """

    states = []
    component_cycles = []
    sequences = []
    assumed_voltage = []

    components = project.components.prefetch_related('modes').order_by('id')

    for component in components:
        voltage = _positive(component.voltage)

        if voltage is None:
            voltage = DEFAULT_VOLTAGE_V
            assumed_voltage.append(component.name)

        sequence = []

        cycle_ms = 0.0
        energy_mah = 0.0
        currents = []

        for mode in component.modes.all():
            current_ma = mode.current_ma
            duration_ms = _positive(mode.duration_ms)
            count = mode.count_per_cycle or 0

            if current_ma is None or current_ma < 0:
                continue

            if duration_ms is None or count <= 0:
                continue

            mode_mah = current_ma * duration_ms * count / MS_PER_HOUR

            states.append({
                'component': component.name,
                'category': component.category,
                'name': mode.name,
                'current_ma': current_ma,
                'duration_ms': duration_ms,
                'count': count,
                'voltage_v': voltage,
                'power_mw': current_ma * voltage,
                'energy_mah': mode_mah,
                'energy_mwh': mode_mah * voltage,
            })

            cycle_ms += duration_ms * count
            energy_mah += mode_mah
            currents.append(current_ma)
            sequence.extend([(duration_ms, current_ma, voltage)] * count)

        if cycle_ms > 0:
            sequences.append(sequence)
            component_cycles.append({
                'name': component.name,
                'voltage_v': voltage,
                'cycle_ms': cycle_ms,
                'energy_mah': energy_mah,
                'energy_mwh': energy_mah * voltage,
                'min_current_ma': min(currents),
                'max_current_ma': max(currents),
            })

    if not states:
        return None

    cycle_ms = max(item['cycle_ms'] for item in component_cycles)
    cycles_per_day = MS_PER_DAY / cycle_ms

    cycle_mah = sum(item['energy_mah'] for item in component_cycles)
    cycle_mwh = sum(item['energy_mwh'] for item in component_cycles)

    daily_mah = cycle_mah * cycles_per_day
    daily_mwh = cycle_mwh * cycles_per_day

    # Les courants ne s'additionnent que lorsque les composants
    # fonctionnent simultanément : lecture sur la courbe en plateaux.
    plateaus = _plateaus(sequences, cycle_ms)

    peak_current_ma = max(item['current_ma'] for item in plateaus)
    min_current_ma = min(item['current_ma'] for item in plateaus)
    peak_power_mw = max(item['power_mw'] for item in plateaus)

    for state in states:
        state['energy_percent'] = (
            state['energy_mwh'] / cycle_mwh * 100 if cycle_mwh > 0 else 0
        )

    states.sort(key=lambda item: item['energy_mwh'], reverse=True)

    return {
        'cycle_duration_ms': cycle_ms,
        'cycles_per_day': cycles_per_day,
        'energy_cycle_mah': cycle_mah,
        'energy_cycle_mwh': cycle_mwh,
        'energy_daily_mah': daily_mah,
        'energy_daily_mwh': daily_mwh,
        'energy_daily_wh': daily_mwh / 1000,
        'energy_monthly_mwh': daily_mwh * DAYS_PER_MONTH,
        'energy_yearly_mwh': daily_mwh * DAYS_PER_YEAR,
        'current_average_ma': cycle_mah * MS_PER_HOUR / cycle_ms,
        'current_min_ma': min_current_ma,
        'current_max_ma': peak_current_ma,
        'average_power_mw': cycle_mwh * MS_PER_HOUR / cycle_ms,
        'peak_power_mw': peak_power_mw,
        'components': component_cycles,
        'states': states,
        'plateaus': plateaus,
        'voltage_assumed_for': assumed_voltage,
        'default_voltage_v': DEFAULT_VOLTAGE_V,
    }


def _plateaus(sequences, cycle_ms):
    """
    Courbe en plateaux du courant total sur un cycle.

    Chaque composant déroule sa séquence de modes depuis t = 0 ; les
    courants simultanés s'additionnent. Après la fin de sa séquence, un
    composant ne consomme plus rien (même hypothèse que le bilan).
    """

    edges = {0.0, cycle_ms}
    timelines = []

    for sequence in sequences:
        t = 0.0
        steps = []

        for duration_ms, current_ma, voltage in sequence:
            steps.append((t, t + duration_ms, current_ma, voltage))
            t += duration_ms
            edges.add(t)

        timelines.append(steps)

    edges = sorted(edge for edge in edges if edge <= cycle_ms)
    plateaus = []

    for start, end in zip(edges, edges[1:]):
        if end <= start:
            continue

        middle = (start + end) / 2
        active = [
            (current, voltage)
            for steps in timelines
            for (s, e, current, voltage) in steps
            if s <= middle < e
        ]
        total = sum(current for current, _voltage in active)
        power = sum(current * voltage for current, voltage in active)

        if (
            plateaus
            and plateaus[-1]['current_ma'] == total
            and plateaus[-1]['power_mw'] == power
        ):
            plateaus[-1]['end_ms'] = end
        else:
            plateaus.append({
                'start_ms': start,
                'end_ms': end,
                'current_ma': total,
                'power_mw': power,
            })

    return plateaus


def relative_gap(reference, other):
    """Écart relatif |other - reference| / reference, ou None."""

    reference = _positive(reference)

    try:
        other = float(other)
    except (TypeError, ValueError):
        return None

    if reference is None:
        return None

    return abs(other - reference) / reference
