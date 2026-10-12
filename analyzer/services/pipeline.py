"""
Workflow d'analyse AIDE-IoT (cahier des charges §5) :

collecte des données → récupération documentaire (RAG) → calcul
énergétique Python → dimensionnement batterie / panneau → vérification
du matériel → interprétation par les agents IA → rapport.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings

from . import agents
from .datasheet_extractor import extract_battery_values_from_datasheet
from .energy_model import compute_energy_profile
from .rag import DocumentIndex, number_passages
from .sizing import STATUS_LABELS, size_system
from .weather_api import get_coordinates_from_city, get_sun_hours


logger = logging.getLogger(__name__)

MONTHS_FR = {
    1: 'janvier', 2: 'février', 3: 'mars', 4: 'avril', 5: 'mai',
    6: 'juin', 7: 'juillet', 8: 'août', 9: 'septembre', 10: 'octobre',
    11: 'novembre', 12: 'décembre',
}


# ============================================================
# COLLECTE
# ============================================================

def resolve_sun_data(project):
    """HSP du site : PVGIS, puis Open-Meteo, sinon valeur signalée."""

    latitude, longitude = project.latitude, project.longitude

    if latitude is None or longitude is None:
        coordinates = get_coordinates_from_city(
            project.location_city,
            project.location_country,
        )
        if coordinates:
            latitude = coordinates['latitude']
            longitude = coordinates['longitude']
            project.latitude, project.longitude = latitude, longitude

    sun = get_sun_hours(latitude, longitude)

    if sun.get('source') != 'default':
        project.sun_hours_annual = sun.get('annual')
        project.sun_hours_worst = sun.get('worst')
        project.save(update_fields=[
            'latitude', 'longitude', 'sun_hours_annual', 'sun_hours_worst',
        ])

    return {
        'worst': sun.get('worst'),
        'annual': sun.get('annual'),
        'worst_month': MONTHS_FR.get(sun.get('worst_month')),
        'source': sun.get('source'),
        'warning': sun.get('warning'),
        'monthly': sun.get('monthly', []),
        'latitude': latitude,
        'longitude': longitude,
    }


def _firmware_code(project):
    firmware = project.firmwares.first()

    if firmware is None:
        return ''

    code = firmware.source_code or ''

    if not code.strip() and firmware.code_file:
        try:
            firmware.code_file.open('rb')
            code = firmware.code_file.read(20000).decode('utf-8', 'replace')
        except OSError:
            code = ''
        finally:
            firmware.code_file.close()

    return code


# ============================================================
# FAITS TRANSMIS AUX AGENTS
# ============================================================

def fr(value, decimals=3):
    """Nombre au format français, sans séparateur de milliers."""

    if value is None:
        return 'non disponible'

    return f'{value:.{decimals}f}'.replace('.', ',')


def _component_facts(project, profile):
    lines = ['COMPOSANTS ET MODES (valeurs saisies par l’utilisateur)']

    for component in project.components.prefetch_related('modes'):
        voltage = (
            f'{fr(component.voltage, 2)} V' if component.voltage
            else 'tension non renseignée'
        )
        lines.append(
            f'- {component.name} ({component.get_category_display()}, {voltage})'
        )
        for mode in component.modes.all():
            lines.append(
                f'    · {mode.name} : {fr(mode.current_ma, 4)} mA pendant '
                f'{fr(mode.duration_ms, 0)} ms, {mode.count_per_cycle} fois '
                'par cycle'
            )

    if profile:
        lines.append('PART D’ÉNERGIE PAR MODE (sur un cycle)')
        for state in profile['states']:
            lines.append(
                f"- {state['component']} / {state['name']} : "
                f"{fr(state['energy_percent'], 1)} %"
            )

    return lines


def _energy_facts(profile):
    if profile is None:
        return [
            'BILAN ÉNERGÉTIQUE : impossible, aucun mode de fonctionnement '
            'exploitable n’est saisi.'
        ]

    return [
        'BILAN ÉNERGÉTIQUE',
        f"- Durée du cycle : {fr(profile['cycle_duration_ms'], 0)} ms",
        f"- Cycles par jour : {fr(profile['cycles_per_day'], 2)}",
        f"- Charge journalière : {fr(profile['energy_daily_mah'], 3)} mAh/jour",
        f"- Énergie journalière : {fr(profile['energy_daily_wh'], 4)} Wh/jour",
        f"- Courant moyen : {fr(profile['current_average_ma'], 4)} mA",
        f"- Courant minimal (veille) : {fr(profile['current_min_ma'], 4)} mA",
        f"- Courant de pointe (simultané) : {fr(profile['current_max_ma'], 2)} mA",
        f"- Puissance moyenne : {fr(profile['average_power_mw'], 3)} mW",
    ]


def _hardware_line(item, kind):
    if not item:
        return f'- {kind} : aucun'

    if kind == 'Batterie':
        return (
            f"- {kind} : {item['name']}, {fr(item['capacity_mah'], 0)} mAh, "
            f"{fr(item['voltage_v'], 2)} V, {fr(item['energy_wh'], 2)} Wh, "
            f"autonomie {fr(item['autonomy_days'], 1)} jours"
        )

    return (
        f"- {kind} : {item['name']}, {fr(item['power_w'], 2)} Wc, production "
        f"{fr(item['daily_production_wh'], 3)} Wh/jour au mois défavorable"
    )


def _sizing_facts(project, sizing, sun):
    decision = sizing['decision']
    lines = [
        f"DÉCISION DU MOTEUR : {decision['title']}",
    ]
    lines += [f'- Raison : {item}' for item in decision['reasons']]
    lines += [f'- Donnée manquante : {item}' for item in decision['missing']]

    lines.append(
        f"SITE : {project.location_city}, {project.location_country} ; "
        f"installation {sizing['installation']}"
    )

    solar_pref = getattr(project, 'solar_pref', None)
    if solar_pref is not None:
        if solar_pref.light_transmittance_percent:
            received = solar_pref.light_transmittance_percent
            lines.append(
                f'- Part de la lumière qui atteint le panneau : {fr(received, 1)} % '
                f'(les {fr(100 - received, 1)} % restants sont bloqués par l’ombrage)'
            )
        if solar_pref.shading_notes:
            lines.append(f'- Ombrage déclaré : {solar_pref.shading_notes}')
        if solar_pref.tilt_deg is not None:
            lines.append(f'- Inclinaison : {fr(solar_pref.tilt_deg, 0)}°')
        if solar_pref.azimuth_deg is not None:
            lines.append(f'- Azimut : {fr(solar_pref.azimuth_deg, 0)}°')

    if sun and sun.get('worst'):
        lines.append(
            f"- Heures de soleil équivalentes, mois le plus défavorable "
            f"({sun.get('worst_month') or 'non précisé'}) : "
            f"{fr(sun['worst'], 2)} h/jour ; moyenne annuelle "
            f"{fr(sun.get('annual'), 2)} h/jour ; source {sun.get('source')}"
        )

    for label, section in (
        ('BATTERIE', sizing['battery']),
        ('PANNEAU', sizing['solar']),
    ):
        if not section:
            continue

        lines.append(
            f"{label} : mode {section['mode']}, statut "
            f"{STATUS_LABELS[section['status']]}"
        )
        if section['margin'] is not None:
            lines.append(
                f"- Marge : {fr(section['margin'], 2)} (rapport sans unité "
                "entre ce que fournit le matériel existant et le besoin ; "
                "1 = juste suffisant ; ce n’est pas un nombre de jours)"
            )
        if section.get('required_w') is not None:
            lines.append(
                f"- Puissance nécessaire sans marge : {fr(section['required_w'], 3)} Wc ; "
                f"avec marge : {fr(section['required_w_with_margin'], 3)} Wc"
            )
        kind = 'Batterie' if label == 'BATTERIE' else 'Panneau'
        if section['existing']:
            lines.append('Existant :')
            lines.append(_hardware_line(section['existing'], kind))
        if section['recommended']:
            lines.append('Recommandé :')
            lines.append(_hardware_line(section['recommended'], kind))
        lines += [f'- {item}' for item in section['warnings']]
        for check in section.get('checks', []):
            lines.append(f"- {check['label']} : {check['detail']}")

    lifecycle = sizing.get('lifecycle')
    if lifecycle:
        lines.append('DURÉE DE VIE ESTIMÉE')
        battery_life = lifecycle['battery']
        if battery_life and battery_life.get('available'):
            if battery_life['rechargeable']:
                lines.append(
                    f"- Batterie : {fr(battery_life['life_years'], 1)} ans "
                    f"({battery_life['detail']}) ; "
                    f"{fr(battery_life['cycles_per_year'], 1)} cycles complets "
                    f"équivalents par an pour {battery_life['cycle_life']} "
                    f"cycles garantis ; vieillissement calendaire "
                    f"{battery_life['calendar_years']} ans"
                )
            else:
                lines.append(
                    f"- Pile : à remplacer tous les "
                    f"{fr(battery_life['life_days'], 0)} jours"
                )
        if lifecycle['battery_replacements'] is not None:
            lines.append(
                f"- Remplacements de batterie sur "
                f"{lifecycle['horizon_years']} ans : "
                f"{lifecycle['battery_replacements']}"
            )
        panel_life = lifecycle['panel']
        if panel_life:
            lines.append(
                f"- Panneau : durée de vie {panel_life['lifespan_years']} ans, "
                f"{fr(panel_life['end_of_life_percent'], 0)} % de sa puissance "
                "en fin de vie ; production suffisante au mois défavorable "
                f"pendant {panel_life['covered_until_year']} ans"
            )

    electronics = sizing['electronics']
    if electronics:
        lines.append('ÉLECTRONIQUE D’ALIMENTATION')
        lines.append(f"- Tension de la charge : {fr(electronics['load_voltage_v'], 2)} V")
        if electronics['battery_range_v']:
            low, high = electronics['battery_range_v']
            lines.append(
                f'- Plage de tension batterie : {fr(low, 2)} à {fr(high, 2)} V'
            )
        lines.append(f"- Convertisseur : {electronics['converter']}")
        lines.append(
            f"- Courant nominal du convertisseur : "
            f"{fr(electronics['converter_rating_a'], 3)} A"
        )
        if electronics['charger']:
            lines.append(f"- Chargeur : {electronics['charger']}")
        if electronics['max_quiescent_ua'] is not None:
            lines.append(
                f"- Courant de repos maximal conseillé : "
                f"{fr(electronics['max_quiescent_ua'], 2)} µA"
            )
        lines += [f'- {item}' for item in electronics['warnings']]

    return lines


# ============================================================
# POINT D'ENTRÉE
# ============================================================

def run_analysis(project):
    started = time.monotonic()
    timings = {}

    def lap(step):
        elapsed = time.monotonic() - started - sum(timings.values())
        timings[step] = round(max(elapsed, 0.0), 2)

    # 1. Collecte
    profile = compute_energy_profile(project)

    battery_pref = getattr(project, 'battery_pref', None)
    battery_datasheet = (
        extract_battery_values_from_datasheet(battery_pref)
        if battery_pref is not None else None
    )

    solar_pref = getattr(project, 'solar_pref', None)
    needs_sun = not (solar_pref and solar_pref.objective == 'none')
    sun = resolve_sun_data(project) if needs_sun and profile else None

    lap('collecte_et_pvgis')

    # 2. Calcul, dimensionnement, vérification, décision (Python)
    sizing = size_system(project, profile, sun, battery_datasheet)
    lap('calcul_python')

    sources = [
        'Modes de fonctionnement saisis par l’utilisateur.',
    ]
    if sun:
        sources.append(
            f"Heures de soleil : {sun['source']} "
            f"({fr(sun['latitude'], 4)}, {fr(sun['longitude'], 4)})."
        )
    if battery_datasheet and battery_datasheet.get('status') == 'extracted':
        sources.append(
            f"Fiche technique batterie : {battery_datasheet['filename']} "
            '(valeurs à valider).'
        )

    report = {
        'version': 2,
        'energy': profile,
        'sun': sun,
        'sizing': sizing,
        'sources': sources,
        'timings': timings,
        'documents': [],
        'ai': {
            'enabled': settings.AI_AGENTS_ENABLED,
            'model': settings.OLLAMA_MODEL,
            'consumption': None,
            'solar': None,
            'error': None,
        },
    }

    if not settings.AI_AGENTS_ENABLED:
        report['duration_s'] = time.monotonic() - started
        return report

    # 3. Récupération documentaire (RAG)
    try:
        index = DocumentIndex(project)

        consumption_queries = [
            f'{component.name} supply current consumption active sleep mode mA'
            for component in project.components.all()
        ] + ['deep sleep current consumption radio transmission']

        technology = (sizing['hardware']['battery'] or {}).get('technology', '')
        solar_queries = [
            f'{technology} battery discharge current cycle life temperature',
            'solar panel peak sun hours efficiency sizing',
            'charger regulator converter quiescent current protection',
        ]

        consumption_docs = index.search(consumption_queries, k=4)
        solar_docs = index.search(solar_queries, k=4)

        passages = number_passages(consumption_docs + solar_docs)
        by_text = {passage['text']: passage for passage in passages}
        consumption_docs = [by_text[item['text']] for item in consumption_docs]
        solar_docs = [by_text[item['text']] for item in solar_docs]

        report['documents'] = [
            {key: passage[key] for key in ('id', 'source', 'page', 'text')}
            for passage in passages
        ]
        report['rag'] = {
            'indexed': index.documents,
            'semantic': index.semantic,
        }

    except Exception as exc:
        logger.exception('RAG indisponible.')
        consumption_docs = solar_docs = []
        report['ai']['error'] = f'RAG indisponible : {exc}'

    lap('rag')

    # 4. Interprétation par les agents
    energy_lines = _energy_facts(profile)
    component_lines = _component_facts(project, profile)

    jobs = [(
        'consumption', 'L’analyste de consommation',
        lambda: agents.run_consumption_analyst(
            '\n'.join(energy_lines + component_lines),
            consumption_docs,
            _firmware_code(project),
        ),
    )]

    if profile is not None:
        jobs.append((
            'solar', 'L’expert du système solaire',
            lambda: agents.run_solar_expert(
                '\n'.join(energy_lines + _sizing_facts(project, sizing, sun)),
                solar_docs,
            ),
        ))

    errors = []

    for key, label, job in jobs:
        try:
            report['ai'][key] = job()
        except Exception as exc:
            logger.exception('%s : échec.', label)
            if 'Connect' in type(exc).__name__:
                errors.append(
                    'Agents IA indisponibles : Ollama ne répond pas. Lancez-le, '
                    f'puis vérifiez que le modèle {settings.OLLAMA_MODEL} est installé.'
                )
                break
            errors.append(f'{label} n’a pas produit de réponse exploitable.')
        lap(f'agent_{key}')

    if errors:
        report['ai']['error'] = ' '.join(errors)

    report['duration_s'] = time.monotonic() - started

    return report
