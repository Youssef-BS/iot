"""
Moteur déterministe de dimensionnement et de décision (cahier des
charges §4.2 à §4.5, annexe F).

Tous les résultats numériques du rapport viennent d'ici ou de
energy_model : les agents IA ne font que les expliquer.
"""

from __future__ import annotations

import math

from .battery_selector import (
    BATTERY_DATABASE,
    compare_batteries,
    verify_battery,
)
from .panel_selector import (
    DEFAULT_SAFETY_FACTOR as PANEL_SAFETY_FACTOR,
    DEFAULT_INDOOR_PRODUCTION_FACTOR as PANEL_INDOOR_FACTOR,
    DEFAULT_SYSTEM_EFFICIENCY as PANEL_SYSTEM_EFFICIENCY,
    PANEL_DATABASE,
    _currency_info,
    compare_panels,
    installation_type_from_placement,
    verify_panel,
)


# Seuils de marge (annexe F) : propositions initiales à valider avec
# l'encadrant, et non des normes universelles.
MARGIN_INSUFFICIENT = 1.0
MARGIN_TIGHT = 1.2
MARGIN_COMFORTABLE = 2.0
MARGIN_OVERSIZED = 3.0

BATTERY_SAFETY_FACTOR = 1.25

# Durée maximale acceptée pour reconstituer l'autonomie après une
# période sans soleil, au mois le plus défavorable (proposition à valider).
RECOVERY_MAX_DAYS = 7

# Tailles commerciales courantes de petits panneaux (W crête).
COMMERCIAL_PANEL_SIZES_W = (
    0.3, 0.5, 1, 1.5, 2, 3, 5, 6, 10, 15, 20, 30, 50, 80, 100, 150, 200,
)

# Plage de tension d'une cellule : (min en décharge, max en charge).
CELL_VOLTAGE_RANGE = {
    'lipo': (3.0, 4.2),
    '18650': (3.0, 4.2),
    'lifepo4': (2.5, 3.65),
    'nimh': (1.0, 1.45),
    'alkaline': (0.9, 1.6),
    'lithium_coin': (2.0, 3.0),
}

CHARGERS = {
    'lipo': 'Chargeur CC/CV Li-ion 4,2 V par cellule, avec entrée solaire',
    '18650': 'Chargeur CC/CV Li-ion 4,2 V par cellule, avec entrée solaire',
    'lifepo4': 'Chargeur CC/CV LiFePO4 3,6 V par cellule (pas un chargeur Li-ion)',
    'nimh': 'Chargeur NiMH avec détection de fin de charge (−ΔV ou température)',
}

DECISION_TITLES = {
    'adapted': 'Votre solution est adaptée',
    'modify': 'Solution à modifier',
    'incomplete': 'Vérification incomplète',
    'proposed': 'Solution dimensionnée',
}


# ============================================================
# OUTILS
# ============================================================

def _pos(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    return value if value > 0 else None


def _fr(value, decimals=2):
    """Nombre avec virgule décimale, pour les messages du rapport."""

    return f'{value:.{decimals}f}'.replace('.', ',')


def classify_margin(margin):
    """Statut d'un élément vérifié selon sa marge (annexe F)."""

    if margin is None:
        return 'incomplete'

    if margin < MARGIN_INSUFFICIENT:
        return 'insufficient'

    if margin > MARGIN_OVERSIZED:
        return 'oversized'

    if margin < MARGIN_TIGHT:
        return 'tight'

    return 'adapted'


STATUS_LABELS = {
    'adapted': 'Adapté',
    'tight': 'Adapté, marge faible',
    'insufficient': 'Insuffisant',
    'oversized': 'Fortement surdimensionné',
    'incompatible': 'Incompatible',
    'incomplete': 'Données insuffisantes',
    'proposed': 'Recommandé',
}

STATUS_NEEDS_CHANGE = {'insufficient', 'oversized', 'incompatible'}


def _keep_if_no_real_gain(section, key, label):
    """
    Un matériel surdimensionné n'est remplacé que si l'alternative
    commerciale est nettement plus petite (au moins 25 %) : sinon le
    changement n'apporte rien et l'existant est conservé.
    """

    existing, recommended = section['existing'], section['recommended']

    if section['status'] != 'oversized' or not existing or not recommended:
        return

    before, after = existing.get(key), recommended.get(key)

    if before and after and after >= before * 0.75:
        section['status'] = 'adapted'
        section['recommended'] = None
        section['reasons'] = []
        section['warnings'].append(
            f'Le {label} est surdimensionné, mais aucune taille commerciale '
            'nettement plus petite n’existe : il est conservé.'
        )


def _commercial_panel_size(required_w):
    for size in COMMERCIAL_PANEL_SIZES_W:
        if size >= required_w:
            return size

    return math.ceil(required_w / 50) * 50


# ============================================================
# BATTERIE
# ============================================================

def _battery_option(option):
    """Projection d'une option du catalogue sur le format du rapport."""

    if not option:
        return None

    cells = option.get('cells_parallel', 1)
    unit_mah = option['capacity_mah'] // cells

    name = f"{option['name']}, {unit_mah} mAh"
    if cells > 1:
        name = f"{cells} × {name} en parallèle"

    return {
        'source': 'catalogue',
        'name': name,
        'technology': option['key'],
        'voltage_v': option['voltage'],
        'capacity_mah': option['capacity_mah'],
        'cells_parallel': cells,
        'required_capacity_mah': option.get('required_capacity_mah'),
        'energy_wh': option['stored_energy_wh'],
        'autonomy_days': option['autonomy_days'],
        'weight_g': option['weight_g'],
        'volume_cm3': option['volume_cm3'],
        'cost': option.get('price_estimate_local'),
        'currency': option.get('currency'),
        'cycle_life': option['cycle_life'],
        'rechargeable': option['rechargeable'],
        'depth_of_discharge': option['depth_of_discharge_percent'] / 100,
        'efficiency': option['system_efficiency_percent'] / 100,
        'valid': option['valid'],
        'reasons_invalid': option['reasons_invalid'],
    }


def _recommend_battery(pref, daily_wh, autonomy_days, preferred):
    comparison = compare_batteries(
        daily_wh=daily_wh,
        autonomy_days=autonomy_days,
        rechargeable_only=getattr(pref, 'rechargeable_only', True),
        max_weight_g=getattr(pref, 'max_weight_g', None),
        max_volume_cm3=getattr(pref, 'max_volume_cm3', None),
        max_budget=getattr(pref, 'max_budget', None),
        preferred_type=preferred,
        safety_factor=BATTERY_SAFETY_FACTOR,
        currency=getattr(pref, 'currency', None),
    )

    chosen = comparison['preferred']

    if not chosen or not chosen['valid']:
        chosen = comparison['best_choice']

    return _battery_option(chosen), comparison['warnings']


def size_battery(pref, profile, solar_enabled, peak_current_ma, datasheet):
    daily_wh = profile['energy_daily_wh']
    autonomy_days = getattr(pref, 'autonomy_days', None) or 3
    objective = getattr(pref, 'objective', 'recommend')

    section = {
        'mode': objective,
        'autonomy_days_requested': autonomy_days,
        'status': 'proposed',
        'margin': None,
        'existing': None,
        'recommended': None,
        'reasons': [],
        'warnings': [],
        'missing': [],
        'checks': [],
    }

    if objective == 'verify':
        voltage = _pos(pref.nominal_voltage_v)
        capacity = _pos(pref.capacity_mah)

        if voltage is None:
            section['missing'].append('Tension nominale de la batterie')
        if capacity is None:
            section['missing'].append('Capacité de la batterie (mAh)')

        if section['missing']:
            section['status'] = 'incomplete'
            return section

        check = verify_battery(
            daily_wh=daily_wh,
            autonomy_days=autonomy_days,
            nominal_voltage_v=voltage,
            capacity_mah=capacity,
            value_scope=pref.value_scope,
            cells_series=pref.cells_series,
            cells_parallel=pref.cells_parallel,
            technology=pref.chosen_technology,
            model_name=pref.chosen_model,
            rechargeable_only=pref.rechargeable_only,
            max_weight_g=pref.max_weight_g,
            max_volume_cm3=pref.max_volume_cm3,
            safety_factor=BATTERY_SAFETY_FACTOR,
        )

        # Recalcul sans les arrondis d'affichage de verify_battery.
        needed_wh = daily_wh * autonomy_days
        useful_wh = (
            check['pack_voltage_v'] * check['pack_capacity_mah'] / 1000
            * check['depth_of_discharge_percent'] / 100
            * check['system_efficiency_percent'] / 100
        )
        margin = useful_wh / needed_wh if needed_wh else None

        technology = pref.chosen_technology or ''

        section['existing'] = {
            'source': 'utilisateur',
            'name': (
                pref.chosen_model
                or check['technology_name']
                or 'Batterie existante'
            ),
            'technology': technology,
            'voltage_v': check['pack_voltage_v'],
            'capacity_mah': check['pack_capacity_mah'],
            'energy_wh': check['stored_energy_wh'],
            'useful_energy_wh': check['useful_energy_wh'],
            'autonomy_days': check['autonomy_days_real'],
            'weight_g': check['estimated_weight_g'],
            'cells_series': check['cells_series'],
            'cells_parallel': check['cells_parallel'],
            'depth_of_discharge': check['depth_of_discharge_percent'] / 100,
            'efficiency': check['system_efficiency_percent'] / 100,
        }

        section['margin'] = margin
        section['status'] = classify_margin(margin)
        section['warnings'].extend(check['warnings'])

        incompatible = [
            reason for reason in check['reasons_invalid']
            if not reason.startswith('Autonomie réelle')
        ]

        if solar_enabled and technology in {'alkaline', 'lithium_coin'}:
            incompatible.append(
                'Pile non rechargeable : elle ne peut pas être rechargée '
                'par le panneau solaire.'
            )

        if incompatible:
            section['status'] = 'incompatible'
            section['reasons'].extend(incompatible)

        if section['status'] == 'insufficient':
            section['reasons'].append(
                f"L'énergie utile couvre {_fr(margin)} fois le besoin de "
                f"{autonomy_days} jour(s) d'autonomie (il faut une marge d’au moins 1)."
            )
        elif section['status'] == 'oversized':
            section['reasons'].append(
                f"L'énergie utile couvre {_fr(margin)} fois le besoin de "
                f"{autonomy_days} jour(s) d'autonomie : batterie fortement "
                f"surdimensionnée (marge supérieure à {MARGIN_OVERSIZED:g})."
            )
        elif section['status'] == 'tight':
            section['warnings'].append(
                f"Marge faible ({_fr(margin)}) : le vieillissement et le "
                "froid peuvent suffire à la rendre insuffisante."
            )

        if section['status'] in STATUS_NEEDS_CHANGE:
            preferred = technology or pref.battery_type
            if solar_enabled and preferred in {'alkaline', 'lithium_coin'}:
                preferred = 'auto'
            section['recommended'], extra = _recommend_battery(
                pref, daily_wh, autonomy_days, preferred,
            )
            section['warnings'].extend(extra)
            _keep_if_no_real_gain(section, 'energy_wh', 'batterie')

    else:
        preferred = getattr(pref, 'battery_type', 'auto') or 'auto'
        section['recommended'], extra = _recommend_battery(
            pref, daily_wh, autonomy_days, preferred,
        )
        section['warnings'].extend(extra)

    # Courant de pointe admissible (§4.2) : uniquement s'il est connu.
    max_discharge_a = _pos(
        (datasheet or {}).get('extracted_data', {}).get(
            'max_discharge_current_a'
        )
    )

    if max_discharge_a is not None and peak_current_ma:
        ratio = max_discharge_a * 1000 / peak_current_ma
        section['checks'].append({
            'label': 'Courant de pointe admissible',
            'ok': ratio >= 1,
            'detail': (
                f'{_fr(max_discharge_a * 1000, 0)} mA admissibles pour un '
                f'pic de {_fr(peak_current_ma)} mA (fiche technique)'
            ),
        })
        if ratio < 1:
            section['status'] = 'incompatible'
            section['reasons'].append(
                'Le courant de pointe dépasse le courant de décharge '
                'maximal indiqué par la fiche technique.'
            )

    return section


# ============================================================
# PANNEAU SOLAIRE
# ============================================================

def size_solar(pref, profile, sun, installation, autonomy_days):
    daily_wh = profile['energy_daily_wh']
    objective = getattr(pref, 'objective', 'recommend')

    tilt = getattr(pref, 'tilt_deg', None)
    azimuth = getattr(pref, 'azimuth_deg', None)

    section = {
        'mode': objective,
        'status': 'proposed',
        'margin': None,
        'existing': None,
        'recommended': None,
        'required_w': None,
        'required_w_with_margin': None,
        'effective_sun_hours': None,
        'reasons': [],
        'warnings': [],
        'missing': [],
        'checks': [],
    }

    sun_worst = _pos((sun or {}).get('worst'))

    if sun_worst is None:
        section['status'] = 'incomplete'
        section['missing'].append(
            "Heures de soleil équivalentes (HSP) du lieu d'installation"
        )
        return section

    # Lumière reçue (ombrage, canopée, vitre). Quand elle est saisie, elle
    # remplace le facteur intérieur générique : la perte n'est comptée
    # qu'une fois (§4.3).
    transmittance = _pos(getattr(pref, 'light_transmittance_percent', None))
    light_kwargs = {}

    if transmittance is not None:
        light_factor = min(transmittance, 100) / 100
        sun_for_sizing = sun_worst * light_factor
        light_kwargs['indoor_production_factor'] = 1.0
    else:
        light_factor = (
            PANEL_INDOOR_FACTOR if installation == 'indoor' else 1.0
        )
        sun_for_sizing = sun_worst

    section['light_factor'] = light_factor
    section['light_measured'] = transmittance is not None

    comparison = compare_panels(
        daily_wh=daily_wh,
        sun_hours_worst=sun_for_sizing,
        installation_type=installation,
        max_surface_cm2=getattr(pref, 'max_surface_cm2', None),
        max_budget=getattr(pref, 'max_budget', None),
        preferred_type=getattr(pref, 'panel_type', 'auto'),
        currency=getattr(pref, 'currency', None),
        tilt_deg=tilt,
        azimuth_deg=azimuth,
        **light_kwargs,
    )

    # Calcul exact (le comparateur arrondit à 0,01 W pour l'affichage) :
    # P = E_jour / (HSP × rendement_global), §4.3.
    effective_hsp = sun_for_sizing * (
        PANEL_INDOOR_FACTOR
        if installation == 'indoor' and transmittance is None else 1.0
    )
    efficiency = comparison['system_efficiency']
    required = daily_wh / (effective_hsp * efficiency)
    required_with_margin = required * comparison['safety_factor']

    # Puissance permettant aussi la recharge en RECOVERY_MAX_DAYS jours :
    # P × HSP × η − E ≥ E × N / R.
    recovery_w = (
        daily_wh * (1 + autonomy_days / RECOVERY_MAX_DAYS)
        / (effective_hsp * efficiency)
        if effective_hsp else 0
    )

    section['required_w'] = required
    section['required_w_with_margin'] = required_with_margin
    section['required_w_recovery'] = recovery_w
    section['effective_sun_hours'] = effective_hsp
    section['warnings'].extend(comparison['warnings'])

    _label, rate_to_eur, _warning = _currency_info(getattr(pref, 'currency', None))

    def recommended_from(option):
        if not option:
            return None

        technology = PANEL_DATABASE[option['key']]
        computed_w = max(required_with_margin, recovery_w)
        commercial_w = _commercial_panel_size(computed_w)

        return {
            'source': 'catalogue',
            'name': option['name'],
            'technology': option['key'],
            'power_w': commercial_w,
            'count': 1,
            'computed_w': computed_w,
            'daily_production_wh': commercial_w * effective_hsp * efficiency,
            'surface_cm2': commercial_w * technology['surface_per_watt_cm2'],
            'cost': commercial_w * technology['price_per_watt'] / rate_to_eur,
            'currency': option.get('currency'),
            'lifespan_years': option['lifespan_years'],
            'valid': option['valid'],
            'reasons_invalid': option['reasons_invalid'],
        }

    if objective == 'verify':
        power = _pos(pref.panel_power_w)

        if power is None:
            section['status'] = 'incomplete'
            section['missing'].append('Puissance crête du panneau existant')
            return section

        check = verify_panel(
            daily_wh=daily_wh,
            sun_hours_worst=sun_for_sizing,
            installation_type=installation,
            **light_kwargs,
            panel_power_w=power,
            panel_count=pref.panel_count,
            technology=pref.chosen_technology,
            model_name=pref.chosen_model,
            vmp_v=pref.vmp_v,
            voc_v=pref.voc_v,
            imp_ma=pref.imp_ma,
            isc_ma=pref.isc_ma,
            max_surface_cm2=pref.max_surface_cm2,
            tilt_deg=tilt,
            azimuth_deg=azimuth,
        )

        margin = check['total_power_w'] / required if required else None

        section['existing'] = {
            'source': 'utilisateur',
            'name': pref.chosen_model or check['technology_name'] or 'Panneau existant',
            'technology': pref.chosen_technology or '',
            'power_w': check['total_power_w'],
            'count': check['panel_count'],
            'daily_production_wh': check['daily_production_wh'],
            'surface_cm2': check['estimated_surface_cm2'],
            'voc_v': pref.voc_v,
        }
        section['margin'] = margin
        section['status'] = classify_margin(margin)

        section['warnings'] = list(dict.fromkeys(
            section['warnings'] + check['warnings']
        ))

        incompatible = [
            reason for reason in check['reasons_invalid']
            if not reason.startswith('Puissance installée')
        ]

        if incompatible:
            section['status'] = 'incompatible'
            section['reasons'].extend(incompatible)

        if section['status'] == 'insufficient':
            section['reasons'].append(
                f'Le panneau fournit {_fr(margin)} fois la puissance '
                'nécessaire au mois le plus défavorable (il faut une marge d’au moins 1).'
            )
        elif section['status'] == 'oversized':
            section['reasons'].append(
                f'Le panneau fournit {_fr(margin)} fois la puissance '
                'nécessaire au mois le plus défavorable : panneau fortement '
                f'surdimensionné (marge supérieure à {MARGIN_OVERSIZED:g}).'
            )
        elif section['status'] == 'tight':
            section['warnings'].append(
                f'Marge faible ({_fr(margin)}) : poussière, ombrage ou '
                'vieillissement peuvent la rendre insuffisante.'
            )

        recovery = recovery_check(section['existing'], profile, autonomy_days)

        if (
            recovery and not recovery['ok']
            and section['status'] not in STATUS_NEEDS_CHANGE
        ):
            section['status'] = 'insufficient'
            section['reasons'].append(
                'Recharge trop lente après des jours sans soleil : '
                + recovery['detail'] + '.'
            )

        if section['status'] in STATUS_NEEDS_CHANGE:
            section['recommended'] = recommended_from(
                comparison['preferred'] or comparison['best_choice']
            )
            _keep_if_no_real_gain(section, 'power_w', 'panneau')

    else:
        best = comparison['preferred']
        if not best or not best['valid']:
            best = comparison['best_choice']
        section['recommended'] = recommended_from(best)

    if section['existing'] and section['status'] not in STATUS_NEEDS_CHANGE:
        final = section['existing']
    else:
        final = section['recommended']

    recovery = recovery_check(final, profile, autonomy_days)
    section['checks'] = [recovery] if recovery else []

    return section


def recovery_check(solar_hw, profile, autonomy_days):
    """
    §4.3 : la production doit reconstituer l'énergie consommée pendant
    la période sans soleil. Jours nécessaires au mois défavorable :
    (E_jour × N_jours) / (production − E_jour).
    """

    if not solar_hw:
        return None

    production = solar_hw.get('daily_production_wh') or 0
    daily = profile['energy_daily_wh']
    surplus = production - daily

    if surplus <= 0:
        return {
            'label': 'Recharge après jours sans soleil',
            'ok': False,
            'days': None,
            'detail': (
                'La production du mois défavorable ne dépasse pas la '
                'consommation : la batterie ne se recharge pas.'
            ),
        }

    days = daily * autonomy_days / surplus
    days_text = f'{days:.1f}'.replace('.', ',')

    return {
        'label': 'Recharge après jours sans soleil',
        'ok': days <= RECOVERY_MAX_DAYS,
        'days': days,
        'detail': (
            f'{days_text} jour(s) de soleil du mois défavorable pour '
            f'reconstituer {autonomy_days} jour(s) d’autonomie '
            f'(objectif ≤ {RECOVERY_MAX_DAYS} jours)'
        ),
    }


# ============================================================
# ÉLECTRONIQUE D'ALIMENTATION (§4.4, annexe C.3)
# ============================================================

def size_electronics(battery_hw, profile, solar_section):
    load_voltage = max(item['voltage_v'] for item in profile['components'])
    peak_ma = profile['current_max_ma']
    sleep_ma = profile['current_min_ma']

    section = {
        'load_voltage_v': load_voltage,
        'peak_current_ma': peak_ma,
        'converter_rating_a': peak_ma * 1.5 / 1000,
        'max_quiescent_ua': sleep_ma * 0.1 * 1000 if sleep_ma > 0 else None,
        'charger': None,
        'converter': None,
        'battery_range_v': None,
        'protections': [
            'Protection contre la surcharge',
            'Protection contre la décharge profonde (coupure basse tension)',
            'Protection contre les surintensités et le court-circuit',
            "Protection contre l'inversion de polarité",
        ],
        'warnings': [],
    }

    if not battery_hw:
        return section

    technology = battery_hw.get('technology') or ''
    cell = CELL_VOLTAGE_RANGE.get(technology)
    cell_nominal = BATTERY_DATABASE.get(technology, {}).get('voltage')

    if technology in {'lipo', '18650', 'lifepo4'}:
        section['protections'].append(
            'Interdiction de charge sous 0 °C (capteur de température)'
        )

    if solar_section is not None:
        section['charger'] = CHARGERS.get(technology)
        if section['charger'] is None and technology:
            section['warnings'].append(
                'Technologie non rechargeable : aucun chargeur solaire '
                'ne peut être utilisé.'
            )
        elif section['charger'] is None:
            section['warnings'].append(
                'Technologie de batterie inconnue : le chargeur ne peut '
                'pas être choisi.'
            )

    if not cell or not cell_nominal:
        section['converter'] = 'À déterminer (technologie de batterie inconnue)'
        return section

    series = max(1, round((battery_hw.get('voltage_v') or cell_nominal) / cell_nominal))
    v_min, v_max = cell[0] * series, cell[1] * series
    section['battery_range_v'] = (v_min, v_max)

    dropout = 0.2

    if v_min >= load_voltage + dropout:
        section['converter'] = (
            'Abaisseur : régulateur LDO à faible courant de repos '
            'ou convertisseur buck'
        )
    elif v_max < load_voltage:
        section['converter'] = 'Élévateur : convertisseur boost'
    else:
        section['converter'] = (
            'Buck-boost : la tension de la batterie traverse la tension '
            'de la charge pendant la décharge'
        )

    return section


# ============================================================
# DURÉE DE VIE ESTIMÉE (batterie et panneau)
# ============================================================

# Vieillissement calendaire typique à 25 °C, en années (ordres de
# grandeur à valider avec l'encadrant et les fiches techniques).
BATTERY_CALENDAR_YEARS = {
    'lipo': 3,
    '18650': 5,
    'lifepo4': 10,
    'nimh': 5,
    'alkaline': 7,
    'lithium_coin': 10,
}

# Fin de vie conventionnelle d'une batterie rechargeable : 80 % de la
# capacité d'origine.
BATTERY_END_OF_LIFE = 0.80

# Dégradation du panneau : (perte la première année, perte annuelle
# ensuite). Le silicium amorphe perd davantage au début (effet
# Staebler-Wronski).
PANEL_DEGRADATION = {
    'monocrystalline': (0.02, 0.005),
    'polycrystalline': (0.025, 0.007),
    'amorphous': (0.10, 0.01),
    'perc': (0.02, 0.0045),
    'bifacial': (0.02, 0.0045),
}
PANEL_DEGRADATION_DEFAULT = (0.025, 0.007)
PANEL_LIFESPAN_DEFAULT = 20

# Horizon d'étude sans panneau solaire, en années.
LIFECYCLE_HORIZON_NO_SOLAR = 10


def panel_factor(year, technology):
    """Fraction de la puissance d'origine restant à la fin de l'année."""

    first, yearly = PANEL_DEGRADATION.get(technology, PANEL_DEGRADATION_DEFAULT)

    if year <= 0:
        return 1.0

    return (1 - first) * (1 - yearly) ** (year - 1)


def battery_lifecycle(hardware, daily_wh, autonomy_requested):
    """
    Durée de vie de la batterie : la plus courte entre le vieillissement
    calendaire et l'usure par les cycles. Toute l'énergie consommée est
    supposée transiter par la batterie (hypothèse prudente).
    """

    if not hardware:
        return None

    technology = hardware.get('technology') or ''
    spec = BATTERY_DATABASE.get(technology)
    energy_wh = _pos(hardware.get('energy_wh'))

    if not spec or not energy_wh:
        return {
            'available': False,
            'detail': 'Technologie ou énergie de la batterie inconnue : '
                      'durée de vie non estimée.',
        }

    calendar_years = BATTERY_CALENDAR_YEARS.get(technology, 5)
    autonomy_days = _pos(hardware.get('autonomy_days'))

    if not spec['rechargeable']:
        life_days = min(autonomy_days or 0, calendar_years * 365)
        return {
            'available': True,
            'rechargeable': False,
            'technology': spec['name'],
            'calendar_years': calendar_years,
            'life_years': life_days / 365 if life_days else None,
            'life_days': life_days or None,
            'replacements_per_year': 365 / life_days if life_days else None,
            'limiting': 'discharge',
            'detail': (
                'Pile non rechargeable : elle est à remplacer à chaque '
                'décharge complète.'
            ),
        }

    cycle_life = spec['cycle_life']
    depth = spec['depth_of_discharge']
    efficiency = spec['system_efficiency']

    # Énergie tirée des cellules par jour et cycles complets équivalents.
    drawn_wh = daily_wh / efficiency
    daily_depth = drawn_wh / energy_wh
    cycles_per_year = 365 * drawn_wh / (energy_wh * depth)
    cycling_years = cycle_life / cycles_per_year if cycles_per_year else None

    if cycling_years is None or calendar_years <= cycling_years:
        life_years, limiting = calendar_years, 'calendar'
    else:
        life_years, limiting = cycling_years, 'cycles'

    end_autonomy = autonomy_days * BATTERY_END_OF_LIFE if autonomy_days else None

    return {
        'available': True,
        'rechargeable': True,
        'technology': spec['name'],
        'cycle_life': cycle_life,
        'calendar_years': calendar_years,
        'daily_depth_percent': daily_depth * 100,
        'cycles_per_year': cycles_per_year,
        'cycling_years': cycling_years,
        'life_years': life_years,
        'limiting': limiting,
        'end_of_life_percent': BATTERY_END_OF_LIFE * 100,
        'end_autonomy_days': end_autonomy,
        'end_autonomy_ok': (
            end_autonomy >= autonomy_requested if end_autonomy else None
        ),
        'detail': (
            'Limitée par le vieillissement calendaire : la batterie s’use '
            'avec le temps plus vite qu’avec les cycles.'
            if limiting == 'calendar' else
            'Limitée par le nombre de cycles de charge et de décharge.'
        ),
    }


def panel_lifecycle(hardware, coverage_now):
    """
    Durée de vie du panneau et année à partir de laquelle sa production
    au mois défavorable ne couvre plus la consommation.
    """

    if not hardware:
        return None

    technology = hardware.get('technology') or ''
    spec = PANEL_DATABASE.get(technology)
    lifespan = spec['lifespan_years'] if spec else PANEL_LIFESPAN_DEFAULT
    first, yearly = PANEL_DEGRADATION.get(technology, PANEL_DEGRADATION_DEFAULT)

    covered_until = lifespan
    if coverage_now is not None:
        for year in range(1, lifespan + 1):
            if coverage_now * panel_factor(year, technology) < 1:
                covered_until = year - 1
                break

    return {
        'technology': spec['name'] if spec else None,
        'technology_known': spec is not None,
        'lifespan_years': lifespan,
        'first_year_loss_percent': first * 100,
        'yearly_loss_percent': yearly * 100,
        'end_of_life_percent': panel_factor(lifespan, technology) * 100,
        'coverage_now': coverage_now,
        'covered_until_year': covered_until,
        'covered_whole_life': covered_until >= lifespan,
    }


def lifecycle_estimate(profile, battery, solar, monthly):
    """Durée de vie estimée du système d'alimentation et calendrier."""

    if profile is None:
        return None

    daily_wh = profile['energy_daily_wh']
    battery_hw = final_hardware(battery)
    solar_hw = final_hardware(solar)

    coverage_now = None
    if monthly and daily_wh:
        coverage_now = min(item['production_wh'] for item in monthly) / daily_wh
    elif solar_hw and daily_wh:
        coverage_now = (solar_hw.get('daily_production_wh') or 0) / daily_wh

    battery_life = battery_lifecycle(
        battery_hw, daily_wh,
        battery['autonomy_days_requested'] if battery else 0,
    )
    panel_life = panel_lifecycle(solar_hw, coverage_now)

    if not battery_life and not panel_life:
        return None

    horizon = (
        panel_life['lifespan_years'] if panel_life
        else LIFECYCLE_HORIZON_NO_SOLAR
    )

    replacements = None
    replacement_years = []
    replacement_cost = None

    if battery_life and battery_life.get('available') and battery_life['life_years']:
        life = battery_life['life_years']
        replacements = max(0, math.ceil(horizon / life - 1e-9) - 1)
        if battery_life['rechargeable']:
            replacement_years = [
                _fr(life * index, 1) for index in range(1, replacements + 1)
            ]
        cost = (battery_hw or {}).get('cost')
        if cost:
            replacement_cost = cost * replacements

    timeline = []
    if panel_life and coverage_now is not None:
        technology = (solar_hw or {}).get('technology') or ''
        years = sorted({1, *range(5, horizon + 1, 5), horizon})
        for year in years:
            factor = panel_factor(year, technology)
            batteries_used = (
                min(
                    math.floor(year / battery_life['life_years'] + 1e-9),
                    replacements,
                )
                if battery_life and battery_life.get('available')
                and battery_life.get('rechargeable') and battery_life['life_years']
                else None
            )
            timeline.append({
                'year': year,
                'panel_percent': factor * 100,
                'production_wh': (
                    (solar_hw.get('daily_production_wh') or 0) * factor
                ),
                'coverage': coverage_now * factor,
                'covered': coverage_now * factor >= 1,
                'battery_replaced': batteries_used,
            })

    return {
        'battery': battery_life,
        'panel': panel_life,
        'horizon_years': horizon,
        'battery_replacements': replacements,
        'battery_replacement_years': replacement_years,
        'battery_replacement_cost': replacement_cost,
        'currency': (battery_hw or {}).get('currency'),
        'timeline': timeline,
    }


# ============================================================
# DÉCISION ET SYNTHÈSE
# ============================================================

def decide(profile, battery, solar):
    """Décision globale (§4.5, annexe F)."""

    missing = []
    reasons = []

    if profile is None:
        missing.append(
            'Modes de fonctionnement (courant, durée et répétitions) : '
            "l'IA n'estime pas les courants."
        )

    for label, section in (('Batterie', battery), ('Panneau', solar)):
        if not section:
            continue

        missing.extend(f'{label} : {item}' for item in section['missing'])

        if section['status'] in STATUS_NEEDS_CHANGE:
            reasons.extend(f'{label} : {item}' for item in section['reasons'])

    if missing:
        code = 'incomplete'
    elif reasons:
        code = 'modify'
    elif any(
        section and section['mode'] == 'verify'
        for section in (battery, solar)
    ):
        code = 'adapted'
    else:
        code = 'proposed'

    return {
        'code': code,
        'title': DECISION_TITLES[code],
        'reasons': reasons,
        'missing': missing,
    }


def final_hardware(section):
    """Matériel à utiliser : l'existant s'il convient, sinon la recommandation."""

    if not section:
        return None

    if section['existing'] and section['status'] not in STATUS_NEEDS_CHANGE:
        return {**section['existing'], 'keep': True}

    if section['recommended']:
        return {**section['recommended'], 'keep': False}

    return None


MONTHS_SHORT = ('janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin',
                'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.')


def monthly_balance(sun, solar, profile):
    """
    Prévision mois par mois : production du panneau retenu (HSP PVGIS du
    mois × puissance × lumière reçue × rendement) face à la consommation.
    """

    hardware = final_hardware(solar)
    months = (sun or {}).get('monthly') or []

    if not hardware or len(months) != 12:
        return []

    factor = solar.get('light_factor', 1.0) * PANEL_SYSTEM_EFFICIENCY
    daily = profile['energy_daily_wh']

    return [
        {
            'month': MONTHS_SHORT[int(item['month']) - 1],
            'production_wh': hardware['power_w'] * item['peak_sun_hours'] * factor,
            'consumption_wh': daily,
        }
        for item in sorted(months, key=lambda entry: int(entry['month']))
    ]


def _days(value):
    if value is None:
        return '—'
    return f'{value:,.0f}'.replace(',', ' ') if value >= 100 else _fr(value, 1)


def action_items(profile, battery, solar, electronics, sun):
    """Recommandations courtes et concrètes, en langage simple."""

    actions = []

    if profile is None:
        return [
            'Saisissez les modes de fonctionnement de chaque composant '
            '(courant, durée, nombre par cycle) : sans eux, aucun bilan '
            'fiable n’est possible.'
        ]

    # --- Batterie ---
    if battery:
        existing, recommended = battery['existing'], battery['recommended']
        days = battery['autonomy_days_requested']

        if battery['status'] == 'incomplete':
            actions.append(
                'Complétez la tension et la capacité de votre batterie pour '
                'qu’elle puisse être vérifiée.'
            )
        elif existing and battery['status'] not in STATUS_NEEDS_CHANGE:
            actions.append(
                f"Gardez votre batterie ({existing['name']}) : elle tient "
                f"{_days(existing['autonomy_days'])} jours sans soleil, pour "
                f"{days} demandés."
            )
        elif existing and recommended:
            if battery['status'] == 'insufficient':
                actions.append(
                    f"Votre batterie ne tient que "
                    f"{_days(existing['autonomy_days'])} jours sans soleil. "
                    f"Passez à {recommended['name']} "
                    f"({_days(recommended['autonomy_days'])} jours)."
                )
            elif battery['status'] == 'incompatible':
                actions.append(
                    f"Changez de batterie ({battery['reasons'][0].rstrip('.')}). "
                    f"Prenez {recommended['name']}."
                )
            else:
                actions.append(
                    f"Votre batterie est bien plus grande que nécessaire. "
                    f"{recommended['name']} suffit : "
                    f"{_days(recommended['autonomy_days'])} jours sans soleil "
                    f"au lieu de {_days(existing['autonomy_days'])}, pour "
                    f"{days} demandés."
                )
        elif recommended:
            actions.append(
                f"Batterie conseillée : {recommended['name']}, soit "
                f"{_days(recommended['autonomy_days'])} jours sans soleil "
                f"(vous en demandiez {days})."
            )

    # --- Panneau ---
    month = (sun or {}).get('worst_month') or 'mois le plus sombre'

    if solar:
        existing, recommended = solar['existing'], solar['recommended']
        consumption = _fr(profile['energy_daily_wh'], 3)

        if solar['status'] == 'incomplete':
            actions.append(
                'Indiquez la puissance de votre panneau, ou demandez une '
                'recommandation.'
            )
        elif existing and solar['status'] not in STATUS_NEEDS_CHANGE:
            actions.append(
                f"Gardez votre panneau de {_fr(existing['power_w'], 1)} W : "
                f"en {month}, il produit "
                f"{_fr(existing['daily_production_wh'], 3)} Wh par jour pour "
                f"{consumption} Wh consommés."
            )
        elif existing and recommended:
            verb = (
                'ne suffit pas' if solar['status'] == 'insufficient'
                else 'est bien plus puissant que nécessaire'
            )
            actions.append(
                f"Votre panneau de {_fr(existing['power_w'], 1)} W {verb}. "
                f"Un panneau de {_fr(recommended['power_w'], 1)} W couvre la "
                f"consommation même en {month} et recharge la batterie "
                'après plusieurs jours couverts.'
            )
        elif recommended:
            actions.append(
                f"Panneau conseillé : {_fr(recommended['power_w'], 1)} W "
                f"({recommended['name']}). En {month}, il produit "
                f"{_fr(recommended['daily_production_wh'], 3)} Wh par jour "
                f"pour {consumption} Wh consommés."
            )

    # --- Électronique ---
    if electronics and electronics.get('converter'):
        converter = electronics['converter'].split(':')[0].strip()
        text = (
            f"Côté électronique : un convertisseur {converter.lower()} "
            f"capable de fournir au moins "
            f"{_fr(electronics['converter_rating_a'] * 1000, 0)} mA"
        )
        if electronics.get('max_quiescent_ua'):
            text += (
                f", avec un courant de repos inférieur à "
                f"{_fr(electronics['max_quiescent_ua'], 1)} µA"
            )
        actions.append(text + '.')

    # --- Où gagner de l'énergie ---
    cycle = profile['cycle_duration_ms']
    for state in profile['states']:
        permanent = state['duration_ms'] * state['count'] >= 0.99 * cycle
        if permanent and state['current_ma'] < 1 and state['energy_percent'] >= 3:
            actions.append(
                f"« {state['component']} » consomme en permanence, même "
                f"quand la carte dort : {_fr(state['energy_percent'], 1)} % de "
                "l’énergie. Le couper quand il ne sert pas (avec un transistor "
                "MOSFET, par exemple) réduirait d’autant la consommation."
            )

    top = profile['states'][0]
    actions.append(
        f"Le poste le plus gourmand est le mode « {top['name']} » de "
        f"« {top['component']} » : {_fr(top['energy_percent'], 1)} % de "
        "l’énergie. C’est là qu’une optimisation rapporte le plus."
    )

    return actions


# Écarts théorie / mesure (propositions à valider avec l'encadrant).
MEASURE_AGREEMENT = 0.10
MEASURE_TOLERANCE = 0.25


def measurement_comparison(profile, battery, solar, monthly, measured_ma):
    """
    Compare le courant moyen mesuré au courant moyen calculé, puis
    recalcule l'autonomie et la couverture solaire avec la mesure.
    """

    if not profile or measured_ma is None or measured_ma <= 0:
        return None

    theoretical = profile['current_average_ma']
    ratio = measured_ma / theoretical
    gap = ratio - 1

    if abs(gap) <= MEASURE_AGREEMENT:
        level = 'agree'
        verdict = 'La mesure confirme le calcul.'
    elif abs(gap) <= MEASURE_TOLERANCE:
        level = 'close'
        verdict = (
            'Écart modéré. Le calcul reste utilisable, mais vérifiez les '
            'durées des modes et les courants de veille.'
        )
    elif gap > 0:
        level = 'far'
        verdict = (
            'La carte consomme nettement plus que prévu. Un mode dure '
            'probablement plus longtemps que saisi, ou un élément manque '
            'au bilan : régulateur, LED, pull-up, capteur resté alimenté.'
        )
    else:
        level = 'far'
        verdict = (
            'La carte consomme nettement moins que prévu. Les courants '
            'saisis sont peut-être des valeurs maximales de fiche technique '
            'plutôt que des valeurs typiques.'
        )

    result = {
        'measured_ma': measured_ma,
        'theoretical_ma': theoretical,
        'measured_ua': measured_ma * 1000,
        'theoretical_ua': theoretical * 1000,
        'gap_percent': gap * 100,
        'level': level,
        'verdict': verdict,
        'measured_daily_mah': measured_ma * 24,
        'battery': None,
        'solar': None,
    }

    battery_hw = final_hardware(battery)
    if battery_hw and battery_hw.get('autonomy_days'):
        days = battery_hw['autonomy_days'] / ratio
        requested = battery['autonomy_days_requested']
        result['battery'] = {
            'days': days,
            'requested': requested,
            'ok': days >= requested,
        }

    if monthly:
        worst = min(monthly, key=lambda item: item['production_wh'])
        need = worst['consumption_wh'] * ratio
        result['solar'] = {
            'month': worst['month'],
            'coverage': worst['production_wh'] / need if need else None,
            'ok': worst['production_wh'] >= need,
        }

    return result


def comparison_rows(battery, solar):
    """Comparaison avant / après pour les éléments vérifiés et modifiés."""

    rows = []

    if battery and battery['existing'] and battery['recommended']:
        before, after = battery['existing'], battery['recommended']
        rows += [
            ('Batterie', before['name'], after['name'], '', 0),
            ('Capacité', before['capacity_mah'], after['capacity_mah'], 'mAh', 0),
            ('Tension', before['voltage_v'], after['voltage_v'], 'V', 2),
            ('Énergie stockée', before['energy_wh'], after['energy_wh'], 'Wh', 2),
            ('Autonomie sans soleil', before['autonomy_days'], after['autonomy_days'], 'jours', 0),
        ]

    if solar and solar['existing'] and solar['recommended']:
        before, after = solar['existing'], solar['recommended']
        rows += [
            ('Panneau', before['name'], after['name'], '', 0),
            ('Puissance crête', before['power_w'], after['power_w'], 'W', 2),
            (
                'Production (mois défavorable)',
                before['daily_production_wh'],
                after['daily_production_wh'],
                'Wh/j',
                3,
            ),
        ]

    return [
        {'label': label, 'before': before, 'after': after, 'unit': unit, 'decimals': decimals}
        for label, before, after, unit, decimals in rows
    ]


def size_system(project, profile, sun, battery_datasheet=None):
    """Point d'entrée : dimensionnement, vérification et décision."""

    if profile is not None and not profile['energy_daily_wh'] > 0:
        profile = None

    battery_pref = getattr(project, 'battery_pref', None)
    solar_pref = getattr(project, 'solar_pref', None)

    solar_enabled = not (solar_pref and solar_pref.objective == 'none')

    placement = getattr(solar_pref, 'placement', '') if solar_pref else ''
    installation = (
        installation_type_from_placement(placement)
        if placement else (project.installation_type or 'outdoor')
    )

    battery = solar = electronics = None

    if profile is not None:
        battery = size_battery(
            battery_pref,
            profile,
            solar_enabled,
            profile['current_max_ma'],
            battery_datasheet,
        )

        if solar_enabled:
            solar = size_solar(
                solar_pref, profile, sun, installation,
                battery['autonomy_days_requested'],
            )

        electronics = size_electronics(
            final_hardware(battery), profile, solar,
        )

    decision = decide(profile, battery, solar)

    hypotheses = [
        f'Facteur de réserve batterie : {BATTERY_SAFETY_FACTOR:g}.',
        'Profondeur de décharge et rendement selon la technologie '
        '(catalogue AIDE-IoT).',
        f'Rendement global de la chaîne solaire : '
        f'{PANEL_SYSTEM_EFFICIENCY:.0%} ; marge panneau : '
        f'{PANEL_SAFETY_FACTOR:g}.',
        f'Seuils de marge : insuffisant < {MARGIN_INSUFFICIENT:g}, adapté '
        f'{MARGIN_TIGHT:g}–{MARGIN_COMFORTABLE:g}, surdimensionné > '
        f'{MARGIN_OVERSIZED:g} (à valider avec l’encadrant).',
        f'Recharge de l’autonomie en {RECOVERY_MAX_DAYS} jours maximum au '
        'mois le plus défavorable (à valider avec l’encadrant).',
        'Composants en parallèle : chaque composant exécute sa séquence '
        'une fois par cycle, le cycle dure autant que la plus longue.',
    ]

    limits = [
        'Le courant de repos du convertisseur et du chargeur n’est pas '
        'inclus dans le bilan tant qu’il n’est pas connu.',
        'Le panneau est supposé installé au même endroit que le dispositif.',
        'Les coûts sont des ordres de grandeur, pas des prix fournisseurs.',
    ]

    if profile and profile['voltage_assumed_for']:
        hypotheses.append(
            f"Tension supposée {profile['default_voltage_v']:g} V pour : "
            + ', '.join(profile['voltage_assumed_for']) + '.'
        )

    if solar and solar.get('light_measured'):
        hypotheses.append(
            f"Lumière reçue par le panneau : {_fr(solar['light_factor'] * 100, 1)} % "
            "(valeur saisie), appliquée à l’ensoleillement du mois le plus "
            "défavorable."
        )

    if sun and sun.get('source') == 'default':
        limits.insert(0,
            'Données solaires indisponibles : HSP par défaut utilisées '
            '(hypothèse). Saisissez la valeur du site pour un résultat fiable.'
        )

    if installation == 'indoor':
        limits.append(
            'Production en intérieur estimée avec un facteur simplifié.'
        )

    monthly = monthly_balance(sun, solar, profile) if profile is not None else []
    lifecycle = lifecycle_estimate(profile, battery, solar, monthly)

    if lifecycle:
        hypotheses.append(
            'Durée de vie : batterie en fin de vie à '
            f'{BATTERY_END_OF_LIFE:.0%} de sa capacité, toute l’énergie '
            'consommée transite par la batterie, vieillissement calendaire '
            'à 25 °C ; panneau dégradé selon sa technologie (perte la '
            'première année puis perte annuelle constante).'
        )
        limits.append(
            'Les durées de vie sont des ordres de grandeur : la chaleur, le '
            'froid et une charge maintenue à 100 % les raccourcissent.'
        )

    return {
        'installation': installation,
        'battery': battery,
        'solar': solar,
        'electronics': electronics,
        'decision': decision,
        'hardware': {
            'battery': final_hardware(battery),
            'solar': final_hardware(solar),
        },
        'comparison': comparison_rows(battery, solar),
        'actions': action_items(profile, battery, solar, electronics, sun),
        'monthly': monthly,
        'lifecycle': lifecycle,
        'hypotheses': hypotheses,
        'limits': limits,
        'status_labels': STATUS_LABELS,
    }
