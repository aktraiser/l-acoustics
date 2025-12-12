#!/usr/bin/env python3
"""
Test rapide pour vérifier que l'extraction IA fonctionne bien.

Prend un article au hasard dans l'index et essaie d'extraire les infos business.
Pratique pour debugger sans tout relancer.

Usage:
    python test_extraction.py
"""

import json
import logging
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from extract_business_info import extract_with_ai_agent

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
        logging.error("local.settings.json introuvable")
        return None


def main():
    """Lance le test."""

    config = load_config()
    if not config:
        return

    # Récup des variables
    search_endpoint = config.get("AI_SEARCH_ENDPOINT")
    search_index = config.get("AI_SEARCH_INDEX")
    search_key = config.get("AI_SEARCH_KEY")
    ai_endpoint = config.get("AI_PROJECT_ENDPOINT")
    agent_name = config.get("AI_AGENT_NAME", "lac-weak-signals")

    required = [search_endpoint, search_index, search_key, ai_endpoint, agent_name]
    if not all(required):
        logging.error("Il manque des variables de config")
        return

    logging.info("=" * 50)
    logging.info("Test de l'extraction business")
    logging.info("=" * 50)

    # On va chercher un article pas encore traité
    logging.info("\n1) Récupération d'un article sample...")

    search_client = SearchClient(
        endpoint=search_endpoint,
        index_name=search_index,
        credential=AzureKeyCredential(search_key)
    )

    # On prend le premier article où competitorNameMain est vide
    results = search_client.search(
        search_text="*",
        filter="competitorNameMain eq null",
        select=["id", "url", "title", "content", "entities", "topics"],
        top=1
    )

    articles = list(results)

    if not articles:
        logging.warning("Pas d'article non traité trouvé. Soit tout est fait, soit l'index est vide.")
        return

    article = articles[0]
    title = article.get('title', 'Sans titre')
    logging.info(f"   Article: {title[:80]}...")

    # On teste l'extraction
    logging.info("\n2) Appel de l'agent IA pour extraction...")

    try:
        extracted = extract_with_ai_agent(
            article=article,
            ai_endpoint=ai_endpoint,
            agent_name=agent_name
        )

        if not extracted:
            logging.error("L'extraction a échoué (retour vide)")
            return

        logging.info("   Extraction OK!")
        logging.info("\n   Données extraites:")
        print(json.dumps(extracted, indent=2, ensure_ascii=False))

        # On teste aussi la mise à jour dans l'index
        logging.info("\n3) Test de mise à jour dans AI Search...")

        doc = {"id": article["id"], **extracted}
        result = search_client.merge_or_upload_documents(documents=[doc])

        if result[0].succeeded:
            logging.info("   Mise à jour OK!")
        else:
            logging.error(f"   Echec: {result[0].error_message}")

        logging.info("\n" + "=" * 50)
        logging.info("Test terminé avec succès!")
        logging.info("=" * 50)

    except Exception as e:
        logging.error(f"Erreur pendant le test: {e}")
        raise


if __name__ == "__main__":
    main()
