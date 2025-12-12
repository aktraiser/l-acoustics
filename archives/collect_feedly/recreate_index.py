#!/usr/bin/env python3
"""
Recrée l'index Azure AI Search from scratch avec le bon schéma.

A utiliser quand on veut repartir de zéro (changement de structure, etc.)

Usage:
    python recreate_index.py
"""

import json
import logging
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchFieldDataType,
    SimpleField,
    SearchableField
)
from azure.core.credentials import AzureKeyCredential

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def load_config():
    """Charge la config depuis local.settings.json"""
    try:
        with open("local.settings.json", "r") as f:
            return json.load(f).get("Values", {})
    except FileNotFoundError:
        logging.error("Fichier local.settings.json pas trouvé")
        return None
    except json.JSONDecodeError:
        logging.error("local.settings.json mal formaté")
        return None


def build_index_fields():
    """
    Construit la liste des champs pour l'index.

    On a regroupé par catégorie pour s'y retrouver plus facilement.
    """
    fields = []

    # --- Champs techniques (les trucs de base) ---
    fields.extend([
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="url", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="origin", type=SearchFieldDataType.String, filterable=True, sortable=True, facetable=True),
        SimpleField(name="published", type=SearchFieldDataType.Int64, filterable=True, sortable=True),
        SimpleField(name="crawled", type=SearchFieldDataType.Int64, filterable=True, sortable=True),
        SimpleField(name="language", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="sourceId", type=SearchFieldDataType.String, filterable=True),
    ])

    # --- Contenu de l'article ---
    fields.extend([
        SearchableField(name="title", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchableField(name="entities", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="topics", type=SearchFieldDataType.String, filterable=True),
    ])

    # --- Infos business (ce qu'on extrait avec l'IA) ---
    fields.extend([
        SimpleField(name="publicationDate", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
        SearchableField(name="vertical", type=SearchFieldDataType.String, filterable=True, facetable=True),
    ])

    # --- Infos sur le lieu/venue ---
    fields.extend([
        SearchableField(name="venueName", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="city", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="country", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="zone", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="venueType", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="capacity", type=SearchFieldDataType.Int32, filterable=True, sortable=True, facetable=True),
    ])

    # --- Infos projet ---
    fields.extend([
        SearchableField(name="projectType", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="projectPhase", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="openingYear", type=SearchFieldDataType.Int32, filterable=True, sortable=True, facetable=True),
        SimpleField(name="openingDate", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
    ])

    # --- Budget / investissement ---
    fields.extend([
        SimpleField(name="investment", type=SearchFieldDataType.Double, filterable=True, sortable=True, facetable=True),
        SimpleField(name="investmentCurrency", type=SearchFieldDataType.String, filterable=True, facetable=True),
    ])

    # --- Les acteurs du projet ---
    fields.extend([
        SearchableField(name="investorOwnerManagement", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="architectConsultantContractor", type=SearchFieldDataType.String, filterable=True),
    ])

    # --- Concurrence (le plus important pour nous) ---
    fields.extend([
        SearchableField(name="competitorNameMain", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="competitorNameOther", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="keyProductsInstalled", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="systemIntegrator", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="otherKeyPlayers", type=SearchFieldDataType.String, filterable=True),
    ])

    # --- Le reste ---
    fields.extend([
        SearchableField(name="additionalInformation", type=SearchFieldDataType.String),
        # Ces 3 champs viennent de l'agent lac-analyst-leads
        SimpleField(name="evaluationScore", type=SearchFieldDataType.Int32, filterable=True, sortable=True, facetable=True),
        SimpleField(name="auditOpportunity", type=SearchFieldDataType.Boolean, filterable=True, facetable=True),
        SearchableField(name="auditOpportunityReason", type=SearchFieldDataType.String),
    ])

    return fields


def main():
    """Point d'entrée principal."""

    config = load_config()
    if not config:
        return

    endpoint = config.get("AI_SEARCH_ENDPOINT")
    index_name = config.get("AI_SEARCH_INDEX")
    api_key = config.get("AI_SEARCH_KEY")

    if not all([endpoint, index_name, api_key]):
        logging.error("Il manque des variables dans local.settings.json (AI_SEARCH_*)")
        return

    logging.info("=" * 50)
    logging.info(f"Recréation de l'index '{index_name}'")
    logging.info("=" * 50)

    client = SearchIndexClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(api_key)
    )

    # D'abord on supprime l'ancien index (si il existe)
    logging.info(f"\nSuppression de l'index existant...")
    try:
        client.delete_index(index_name)
        logging.info("  -> Supprimé")
    except Exception as e:
        logging.info(f"  -> N'existait pas (ou erreur: {e})")

    # Ensuite on recrée avec le nouveau schéma
    logging.info(f"\nCréation du nouvel index...")

    fields = build_index_fields()
    new_index = SearchIndex(name=index_name, fields=fields)

    try:
        result = client.create_index(new_index)
        logging.info(f"  -> OK! {len(result.fields)} champs créés")

        # Petit recap
        logging.info("\nChamps créés:")
        for f in result.fields:
            logging.info(f"  - {f.name} ({f.type})")

        logging.info("\n" + "=" * 50)
        logging.info("C'est bon, l'index est prêt!")
        logging.info("Tu peux maintenant lancer: python migrate_and_reload.py")
        logging.info("=" * 50)

    except Exception as e:
        logging.error(f"Erreur lors de la création: {e}")
        raise


if __name__ == "__main__":
    main()
