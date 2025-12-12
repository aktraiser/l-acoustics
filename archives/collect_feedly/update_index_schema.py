#!/usr/bin/env python3
"""
Script pour mettre à jour le schéma de l'index Azure AI Search.
Ajoute tous les champs manquants sans supprimer les données existantes.

Usage:
    python update_index_schema.py
"""

import json
import logging
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField
)
from azure.core.credentials import AzureKeyCredential

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def main():
    """Fonction principale pour mettre à jour le schéma."""

    # Charger les variables depuis local.settings.json
    try:
        with open("local.settings.json", "r") as f:
            settings = json.load(f)
            values = settings.get("Values", {})
    except FileNotFoundError:
        logging.error("❌ Fichier local.settings.json introuvable")
        return
    except json.JSONDecodeError:
        logging.error("❌ Erreur de parsing du fichier local.settings.json")
        return

    SEARCH_ENDPOINT = values.get("AI_SEARCH_ENDPOINT")
    SEARCH_INDEX = values.get("AI_SEARCH_INDEX")
    SEARCH_KEY = values.get("AI_SEARCH_KEY")

    if not all([SEARCH_ENDPOINT, SEARCH_INDEX, SEARCH_KEY]):
        logging.error("❌ Erreur: Variables d'environnement manquantes")
        return

    logging.info("=" * 60)
    logging.info("🔧 Mise à jour du schéma de l'index AI Search")
    logging.info("=" * 60)

    # Créer le client pour gérer les index
    index_client = SearchIndexClient(
        endpoint=SEARCH_ENDPOINT,
        credential=AzureKeyCredential(SEARCH_KEY)
    )

    try:
        # Récupérer l'index existant
        logging.info(f"\n📋 Récupération de l'index '{SEARCH_INDEX}'...")
        existing_index = index_client.get_index(SEARCH_INDEX)
        logging.info(f"   ✅ Index trouvé avec {len(existing_index.fields)} champs existants")

        # Créer la liste des champs existants
        existing_field_names = {field.name for field in existing_index.fields}
        logging.info(f"   📝 Champs existants: {', '.join(sorted(existing_field_names))}")

        # Définir tous les champs requis
        all_fields = [
            # Technical fields (existants + nouveaux)
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=False, sortable=False),
            SimpleField(name="url", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="origin", type=SearchFieldDataType.String, filterable=True, sortable=True, facetable=True),
            SimpleField(name="published", type=SearchFieldDataType.Int64, filterable=True, sortable=True),
            SimpleField(name="crawled", type=SearchFieldDataType.Int64, filterable=True, sortable=True),
            SimpleField(name="language", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="sourceId", type=SearchFieldDataType.String, filterable=True),

            # Content fields (existants)
            SearchableField(name="title", type=SearchFieldDataType.String),
            SearchableField(name="content", type=SearchFieldDataType.String),

            # Metadata fields (existants)
            SearchableField(name="entities", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="topics", type=SearchFieldDataType.String, filterable=True),

            # Business Intelligence fields (NOUVEAUX)
            SimpleField(name="publicationDate", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
            SearchableField(name="competitorNameMain", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SearchableField(name="competitorNameOther", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="venueName", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SearchableField(name="city", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SearchableField(name="country", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="zone", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="capacity", type=SearchFieldDataType.Int32, filterable=True, sortable=True, facetable=True),
            SearchableField(name="venueType", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SearchableField(name="keyProductsInstalled", type=SearchFieldDataType.String, filterable=True),
            SimpleField(name="installationYear", type=SearchFieldDataType.Int32, filterable=True, sortable=True, facetable=True),
            SimpleField(name="installationFullDate", type=SearchFieldDataType.DateTimeOffset, filterable=True, sortable=True),
            SearchableField(name="systemIntegrator", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SearchableField(name="otherKeyPlayers", type=SearchFieldDataType.String, filterable=True),
        ]

        # Identifier les nouveaux champs
        new_fields = [field for field in all_fields if field.name not in existing_field_names]

        if not new_fields:
            logging.info("\n✅ Tous les champs sont déjà présents dans l'index")
            logging.info("   Aucune mise à jour nécessaire")
            return

        logging.info(f"\n📝 {len(new_fields)} nouveaux champs à ajouter:")
        for field in new_fields:
            logging.info(f"   - {field.name} ({field.type})")

        # Mettre à jour l'index avec tous les champs
        updated_index = SearchIndex(
            name=SEARCH_INDEX,
            fields=all_fields
        )

        logging.info(f"\n🚀 Mise à jour de l'index en cours...")
        result = index_client.create_or_update_index(updated_index)
        logging.info(f"   ✅ Index mis à jour avec succès!")
        logging.info(f"   📊 Total: {len(result.fields)} champs dans l'index")

        # Afficher le résumé
        logging.info("\n" + "=" * 60)
        logging.info("✅ Mise à jour terminée avec succès!")
        logging.info("=" * 60)
        logging.info(f"📊 Résumé:")
        logging.info(f"   - Champs existants: {len(existing_field_names)}")
        logging.info(f"   - Nouveaux champs ajoutés: {len(new_fields)}")
        logging.info(f"   - Total de champs: {len(result.fields)}")
        logging.info("=" * 60)
        logging.info("\n✅ Vous pouvez maintenant relancer: python migrate_and_reload.py")

    except Exception as e:
        logging.error(f"\n❌ Erreur lors de la mise à jour de l'index: {e}")
        raise


if __name__ == "__main__":
    main()
