"""
Agents IA d'AIDE-IoT (LangChain + Ollama), cahier des charges §5.

Deux agents spécialisés interprètent et expliquent les résultats :
- l'analyste de consommation IoT (programme, composants, modes) ;
- l'expert du système solaire (site, ensoleillement, panneau, batterie).

Ils ne réalisent aucun calcul numérique. Une garde vérifie que chaque
nombre écrit par un agent figure dans les faits calculés ou dans les
extraits documentaires ; sinon la phrase est retirée.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings
from langchain_core.exceptions import OutputParserException
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


# ============================================================
# PROMPTS
# ============================================================

COMMON_RULES = """\
RÈGLES ABSOLUES
1. Tu ne fais AUCUN calcul. Tous les nombres sont fournis dans « FAITS \
CALCULÉS » par le moteur de calcul Python déterministe. Tu peux recopier \
un nombre fourni, jamais en produire un nouveau : pas d'addition, de \
conversion d'unité, d'arrondi différent ni d'estimation.
2. Tu n'inventes jamais un courant, une tension, une capacité, une \
caractéristique technique ou un résultat. Si une information n'est ni \
dans les faits ni dans les extraits documentaires, indique qu'elle est \
manquante.
3. Quand tu t'appuies sur un extrait documentaire, cite son identifiant \
entre crochets, par exemple [S2]. N'invente jamais d'identifiant.
4. Tu ne changes pas la décision : elle est rendue par le moteur de \
calcul. Tu l'expliques et tu la justifies.
5. Réponds en français, avec des phrases courtes et précises, pour un \
concepteur IoT qui n'est pas expert en énergie.
6. Réponds uniquement avec l'objet JSON demandé, en texte brut : pas de \
Markdown (pas d'astérisques, de dièses ni d'accents graves)."""

CONSUMPTION_ANALYST_PROMPT = f"""\
Tu es l'analyste de consommation IoT de la plateforme AIDE-IoT.

TON RÔLE
Tu examines le programme embarqué, les composants et leurs modes de \
fonctionnement pour expliquer au concepteur combien son dispositif \
consomme et pourquoi.

TA MISSION
- Identifier dans le programme et les modes les phases du cycle : \
réveil et initialisation, mesure des capteurs, traitement, transmission \
(Wi-Fi, Bluetooth, LoRa), réception ou écoute réseau, veille ou deep sleep.
- Expliquer quelle phase domine la consommation, en t'appuyant sur la \
part d'énergie de chaque mode fournie dans les faits.
- Signaler ce qui, dans le programme, influence la consommation \
(attente active au lieu d'une veille, radio laissée allumée, fréquence \
des transmissions...), uniquement si c'est visible dans le code fourni.
- Lister les données manquantes ou peu fiables pour le bilan.

HORS DE TON PÉRIMÈTRE
La batterie, le panneau solaire, le site et l'électronique d'alimentation \
sont traités par l'expert du système solaire : ne les commente pas et ne \
les liste pas comme données manquantes.

{COMMON_RULES}"""

SOLAR_EXPERT_PROMPT = f"""\
Tu es l'expert du système solaire de la plateforme AIDE-IoT.

TON RÔLE
Tu analyses le site d'installation, l'ensoleillement, le panneau, la \
batterie et l'électronique d'alimentation pour expliquer la décision \
rendue par le moteur de calcul.

TA MISSION
- Expliquer la décision (« Votre solution est adaptée », « Solution à \
modifier », « Vérification incomplète » ou « Solution dimensionnée ») à \
partir des marges et des vérifications fournies.
- Justifier la batterie : technologie, énergie utile, autonomie, courant \
de pointe, compatibilité avec la recharge solaire.
- Justifier le panneau : heures de soleil du mois le plus défavorable, \
puissance nécessaire, production, contraintes d'installation.
- Justifier l'électronique : chargeur compatible avec la chimie, \
convertisseur adapté aux tensions, protections, courant de repos.
- Signaler les points d'attention (froid, ombrage, poussière, \
vieillissement) et les données manquantes.

{COMMON_RULES}"""

HUMAN_TEMPLATE = """\
FAITS CALCULÉS (moteur Python, valeurs exactes) :
{facts}

EXTRAITS DOCUMENTAIRES (RAG) :
{documents}
{extra}"""


# ============================================================
# FORMATS DE SORTIE
# ============================================================

class Phase(BaseModel):
    nom: str = Field(description='Nom de la phase du cycle')
    role: str = Field(description='Ce que fait le dispositif pendant cette phase')


class ConsumptionAnalysis(BaseModel):
    phases: list[Phase] = Field(
        description='Phases du cycle identifiées dans le programme et les modes',
    )
    explication_bilan: str = Field(
        description='Pourquoi le dispositif consomme cette énergie (3 à 5 phrases)',
    )
    phase_dominante: str = Field(
        description='La phase qui consomme le plus et pourquoi (1 à 2 phrases)',
    )
    observations_programme: list[str] = Field(
        default_factory=list,
        description='Éléments du code qui influencent la consommation',
    )
    points_attention: list[str] = Field(default_factory=list)
    donnees_manquantes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list, description='Identifiants cités, ex. S1')


class SolarExpertise(BaseModel):
    explication_decision: str = Field(
        description='Pourquoi le moteur rend cette décision (3 à 5 phrases)',
    )
    justification_batterie: str
    justification_panneau: str
    justification_electronique: str
    points_attention: list[str] = Field(default_factory=list)
    donnees_manquantes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list, description='Identifiants cités, ex. S1')


# ============================================================
# GARDE : AUCUN NOMBRE INVENTÉ
# ============================================================

NUMBER_RE = re.compile(r'(?<![\w.,])(\d+(?:[.,]\d+)?)')
SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')
MARKDOWN_RE = re.compile(r'\*\*|__|`|^#+\s*|^\s*[-*]\s+', re.MULTILINE)


DECISION_SPELLING = {
    'Solution a modifier': 'Solution à modifier',
    'Votre solution est adaptee': 'Votre solution est adaptée',
    'Verification incomplete': 'Vérification incomplète',
    'Solution dimensionnee': 'Solution dimensionnée',
}


def plain_text(text):
    """Retire le Markdown et rétablit l'orthographe des décisions."""

    text = MARKDOWN_RE.sub('', text or '').strip()
    for wrong, right in DECISION_SPELLING.items():
        text = text.replace(wrong, right)
    return text


def _numbers(text):
    values = []

    for raw in NUMBER_RE.findall(text or ''):
        try:
            values.append((float(raw.replace(',', '.')), raw))
        except ValueError:
            continue

    return values


class NumberGuard:
    """Vérifie que les nombres d'un texte proviennent des faits fournis."""

    def __init__(self, *sources):
        allowed = set()

        for source in sources:
            for value, _raw in _numbers(source):
                allowed.update({value, value * 1000, value / 1000, value * 100})

        self.allowed = allowed
        self.removed = 0

    def _is_known(self, value, raw):
        if value.is_integer() and value <= 10:
            return True

        decimals = len(raw.split(',')[-1].split('.')[-1]) if (
            ',' in raw or '.' in raw
        ) else 0
        rounding = 0.5 * 10 ** -decimals

        return any(
            abs(value - known) <= max(0.01 * abs(known), rounding)
            for known in self.allowed
        )

    def is_clean(self, text):
        return all(self._is_known(value, raw) for value, raw in _numbers(text))

    def clean_text(self, text):
        kept = []

        for sentence in SENTENCE_RE.split(plain_text(text)):
            if self.is_clean(sentence):
                kept.append(sentence)
            else:
                self.removed += 1

        return ' '.join(kept).strip()

    def clean_list(self, items):
        kept = []

        for item in items or []:
            item = plain_text(item)
            if self.is_clean(item):
                kept.append(item.strip())
            else:
                self.removed += 1

        return [item for item in kept if item]


# ============================================================
# EXÉCUTION
# ============================================================

def _llm():
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=settings.OLLAMA_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=0,
        num_ctx=8192,
        num_predict=1500,
        # Évite les boucles de répétition observées sur les petits modèles.
        repeat_penalty=1.15,
        reasoning=False,
        keep_alive='15m',
        client_kwargs={'timeout': settings.OLLAMA_TIMEOUT_SECONDS},
    )


def format_documents(passages):
    if not passages:
        return 'Aucun extrait disponible.'

    lines = []

    for passage in passages:
        where = passage['source']
        if passage.get('page'):
            where += f", page {passage['page']}"
        lines.append(f"[{passage['id']}] ({where}) {passage['text']}")

    return '\n\n'.join(lines)


def _run(system_prompt, schema, facts, passages, extra=''):
    prompt = ChatPromptTemplate.from_messages([
        ('system', '{system}'),
        ('human', HUMAN_TEMPLATE),
    ])

    chain = prompt | _llm().with_structured_output(schema, method='json_schema')

    documents = format_documents(passages)

    inputs = {
        'system': system_prompt,
        'facts': facts,
        'documents': documents,
        'extra': extra,
    }

    # Un petit modèle produit parfois un JSON invalide (boucle de
    # répétition) : une seconde tentative suffit en général.
    try:
        answer = chain.invoke(inputs)
    except OutputParserException:
        logger.warning('Réponse IA invalide, nouvelle tentative.')
        answer = chain.invoke(inputs)

    guard = NumberGuard(facts, documents, extra)
    known_ids = {passage['id'] for passage in passages}

    cleaned = {}

    for key, value in answer.model_dump().items():
        if key == 'sources':
            cleaned[key] = sorted(
                {item.strip('[] ') for item in value} & known_ids,
                key=lambda item: int(item[1:]),
            )
        elif key == 'phases':
            cleaned[key] = [
                {'nom': plain_text(phase['nom']), 'role': guard.clean_text(phase['role'])}
                for phase in value
                if guard.is_clean(phase['nom'])
            ]
        elif isinstance(value, list):
            cleaned[key] = guard.clean_list(value)
        else:
            cleaned[key] = guard.clean_text(value)

    cleaned['removed_sentences'] = guard.removed

    return cleaned


def run_consumption_analyst(facts, passages, firmware_code=''):
    extra = ''

    if firmware_code.strip():
        extra = (
            '\nPROGRAMME EMBARQUÉ (extrait) :\n```\n'
            + firmware_code[:6000]
            + '\n```'
        )

    return _run(
        CONSUMPTION_ANALYST_PROMPT,
        ConsumptionAnalysis,
        facts,
        passages,
        extra,
    )


def run_solar_expert(facts, passages):
    return _run(SOLAR_EXPERT_PROMPT, SolarExpertise, facts, passages)
