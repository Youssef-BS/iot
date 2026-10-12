import io
import logging
import re
from pathlib import Path

from pypdf import PdfReader


logger = logging.getLogger(__name__)


def extract_component_values_from_datasheet(component):
    """
    Valeurs techniques d'un composant extraites de sa fiche technique.

    Aucune extraction automatique n'est faite : un modèle de langage ne
    doit pas produire de courants ou de caractéristiques (cahier des
    charges §5). Les fiches sont indexées par le RAG et citées comme
    sources dans le rapport ; les valeurs restent saisies par
    l'utilisateur.
    """
    return {}


def extract_mode_values_from_datasheet(component, mode_name):
    """Voir extract_component_values_from_datasheet."""
    return {}


def extract_battery_values_from_datasheet(battery_preference):
    """
    Lit localement le PDF de batterie.

    Les valeurs sont extraites à partir de libellés et d'unités explicites.
    Une valeur absente ou ambiguë n'est pas inventée.

    Cette extraction ne détermine pas le montage série/parallèle du pack
    et ne remplace pas une vérification humaine de la datasheet.
    """
    datasheet = getattr(
        battery_preference,
        "battery_datasheet",
        None,
    )

    result = {
        "provided": bool(datasheet),
        "status": "not_provided",
        "source": "datasheet" if datasheet else None,
        "filename": (
            Path(datasheet.name).name
            if datasheet
            else None
        ),
        "extraction_method": "local_pdf_label_rules",
        "extracted_data": {},
        "source_text": {},
        "field_sources": {},
        "confidence": None,
        "requires_manual_validation": True,
        "warnings": [],
    }

    if not datasheet:
        return result

    try:
        # Lecture limitée à 10 Mo, sans dépendre d'un chemin local.
        try:
            datasheet.open("rb")
            content = datasheet.read(
                10 * 1024 * 1024 + 1
            )
        finally:
            datasheet.close()

        if len(content) > 10 * 1024 * 1024:
            raise ValueError(
                "La datasheet dépasse la taille maximale de 10 Mo."
            )

        if not content.startswith(b"%PDF-"):
            raise ValueError(
                "Le fichier ne possède pas une signature PDF valide."
            )

        reader = PdfReader(
            io.BytesIO(content),
            strict=False,
        )

        if reader.is_encrypted:
            if not reader.decrypt(""):
                raise ValueError(
                    "Le PDF est protégé par un mot de passe."
                )

        if len(reader.pages) > 50:
            raise ValueError(
                "Le PDF dépasse la limite de 50 pages."
            )

        pages = []

        for page_number, page in enumerate(
            reader.pages,
            start=1,
        ):
            text = page.extract_text() or ""

            # Normalisation des espaces et des retours à la ligne.
            text = re.sub(r"\s+", " ", text).strip()

            if text:
                pages.append(
                    (page_number, text)
                )

        if not pages:
            result["status"] = "no_text_found"
            result["warnings"].append(
                "Aucun texte exploitable n'a été trouvé. "
                "Le PDF peut être scanné et nécessiter un traitement OCR."
            )
            return result

        number = r"(?P<value>\d+(?:[.,]\d+)?)"

        # Les motifs associent une valeur à un libellé précis.
        # Ils ne récupèrent pas simplement le premier nombre du document.
        rules = {
            "capacity_mah": (
                r"(?:nominal|rated)\s+capacity\s*[:=]?\s*"
                + number
                + r"\s*(?P<unit>mAh|Ah)\b"
            ),
            "nominal_voltage_v": (
                r"nominal\s+voltage\s*[:=]?\s*"
                + number
                + r"\s*(?P<unit>V)\b"
            ),
            "charge_voltage_v": (
                r"charging\s+voltage\s*[:=]?\s*"
                + number
                + r"\s*(?:±\s*\d+(?:[.,]\d+)?\s*)?"
                + r"(?P<unit>V)\b"
            ),
            "cutoff_voltage_v": (
                r"discharge\s+cut[- ]?off\s+voltage\s*[:=]?\s*"
                + number
                + r"\s*(?P<unit>V)\b"
            ),
            "max_charge_current_a": (
                r"max\.?\s*charge\s+current\s*[:=]?\s*"
                + number
                + r"\s*(?P<unit>mA|A)\b"
            ),
            "max_discharge_current_a": (
                r"max\.?\s*discharge\s+current\s*[:=]?\s*"
                + number
                + r"\s*(?P<unit>mA|A)\b"
            ),
            "weight_g": (
                r"cell\s+weight\s*[:=]?\s*"
                + number
                + r"\s*(?P<unit>g)\b"
            ),
        }

        extracted = result["extracted_data"]

        for field_name, pattern in rules.items():
            candidates = []

            for page_number, text in pages:
                for match in re.finditer(
                    pattern,
                    text,
                    flags=re.IGNORECASE,
                ):
                    raw_value = match.group("value")
                    original_unit = match.group("unit")
                    unit = original_unit.lower()

                    # Une virgule peut séparer les milliers ou les
                    # décimales. Dans les capacités et courants en mA,
                    # « 3,100 mA » vaut 3100 mA ; « 0,68 mA » vaut
                    # 0,68 mA. La forme ambiguë « 1,000 » doit être
                    # confirmée par la preuve de la fiche technique.
                    if (
                        field_name in {
                            "capacity_mah",
                            "max_charge_current_a",
                            "max_discharge_current_a",
                        }
                        and unit in {"mah", "ma"}
                        and re.fullmatch(
                            r"[1-9]\d{0,2}(?:,\d{3})+",
                            raw_value,
                        )
                    ):
                        value = float(raw_value.replace(",", ""))
                    else:
                        value = float(raw_value.replace(",", "."))

                    # Conversion vers les unités attendues par le payload.
                    if unit == "ma":
                        value /= 1000.0
                    elif unit == "ah":
                        value *= 1000.0

                    if value <= 0:
                        continue

                    evidence = text[
                        max(0, match.start() - 20):
                        min(len(text), match.end() + 45)
                    ]

                    candidates.append({
                        "value": value,
                        "page": page_number,
                        "original_unit": original_unit,
                        "text": evidence,
                    })

            unique_values = {
                candidate["value"]
                for candidate in candidates
            }

            if len(unique_values) > 1:
                result["warnings"].append(
                    f"Plusieurs valeurs différentes ont été trouvées "
                    f"pour {field_name}. Ce champ doit être vérifié."
                )
                continue

            if not candidates:
                continue

            candidate = candidates[0]

            extracted[field_name] = candidate["value"]

            result["source_text"][field_name] = (
                candidate["text"]
            )

            result["field_sources"][field_name] = {
                "source": "datasheet",
                "page": candidate["page"],
                "original_unit": candidate["original_unit"],
                "requires_manual_validation": True,
            }

        # Recherche du modèle dans les libellés de couverture/en-tête.
        model_candidates = []

        for page_number, text in pages:
            for match in re.finditer(
                r"(?:Model\s*:|Spec\.\s*No\.)\s*"
                r"([A-Za-z][A-Za-z0-9._/-]*\d[A-Za-z0-9._/-]*)",
                text,
                flags=re.IGNORECASE,
            ):
                model_candidates.append({
                    "value": match.group(1),
                    "page": page_number,
                    "text": match.group(0),
                })

        models = {
            candidate["value"].upper()
            for candidate in model_candidates
        }

        if len(models) == 1:
            candidate = model_candidates[0]
            extracted["model"] = candidate["value"]

            result["source_text"]["model"] = candidate["text"]

            result["field_sources"]["model"] = {
                "source": "datasheet",
                "page": candidate["page"],
                "requires_manual_validation": True,
            }

        elif len(models) > 1:
            result["warnings"].append(
                "Plusieurs modèles apparaissent dans le document. "
                "Le modèle de batterie doit être confirmé."
            )

        # Identification limitée à des mentions explicites.
        # Aucun fabricant ou type n'est déduit du nom du fichier.
        for page_number, text in pages:
            manufacturer = re.search(
                r"\bSamsung\s+SDI\b",
                text,
                flags=re.IGNORECASE,
            )

            if manufacturer and "manufacturer" not in extracted:
                extracted["manufacturer"] = "Samsung SDI"
                result["source_text"]["manufacturer"] = (
                    manufacturer.group(0)
                )
                result["field_sources"]["manufacturer"] = {
                    "source": "datasheet",
                    "page": page_number,
                    "requires_manual_validation": True,
                }

            chemistry = re.search(
                r"\blithium[-\s]+ion\b",
                text,
                flags=re.IGNORECASE,
            )

            if chemistry and "technology" not in extracted:
                extracted["technology"] = "Li-ion"
                extracted["chemistry"] = "Lithium-ion"

                for field_name in ("technology", "chemistry"):
                    result["source_text"][field_name] = (
                        chemistry.group(0)
                    )
                    result["field_sources"][field_name] = {
                        "source": "datasheet",
                        "page": page_number,
                        "requires_manual_validation": True,
                    }

        numeric_fields = set(rules).intersection(extracted)

        result["status"] = (
            "extracted"
            if numeric_fields
            else "no_values_found"
        )

        if not numeric_fields:
            result["warnings"].append(
                "Du texte a été lu, mais aucun libellé numérique "
                "pris en charge n'a été reconnu."
            )

        for field_name in (
            "nominal_voltage_v",
            "capacity_mah",
        ):
            if field_name not in extracted:
                result["warnings"].append(
                    f"Le champ essentiel {field_name} "
                    "n'a pas pu être extrait sans ambiguïté."
                )

        if "weight_g" in extracted:
            result["warnings"].append(
                "Vérifier le qualificatif de masse dans la preuve : "
                "la valeur peut être une limite maximale."
            )

        result["warnings"].append(
            "Les valeurs extraites ne définissent pas le nombre "
            "de cellules ni leur montage série/parallèle."
        )

        logger.info(
            "Extraction locale de la batterie : statut=%s, champs=%s",
            result["status"],
            ", ".join(sorted(extracted)),
        )

        return result

    except Exception:
        logger.exception(
            "Échec de l'extraction locale de la datasheet batterie."
        )

        result["status"] = "extraction_failed"
        result["warnings"].append(
            "Impossible de lire ou d'exploiter la datasheet batterie. "
            "Consulter les journaux Django."
        )

        return result

