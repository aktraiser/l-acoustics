#!/usr/bin/env python3
"""
Traite tous les articles de l'index avec l'agent IA.

Ce script:
1. Récupère tous les articles pas encore enrichis
2. Les passe un par un à l'agent lac-weak-signals
3. Met à jour l'index avec les infos extraites

C'est le script principal pour le batch processing.

Usage:
    python process_all_articles.py
"""

import json
import logging
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

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
    except json.JSONDecodeError:
        logging.error("local.settings.json mal formaté")
        return None


def extract_with_ai_agent(article: dict, ai_endpoint: str, agent_name: str) -> dict | None:
    """
    Envoie un article à l'agent IA et récupère les infos business extraites.

    L'agent retourne du JSON avec les champs comme venueName, city, competitorNameMain, etc.
    """

    # On construit le prompt avec les infos de l'article
    # On tronque le contenu à 4000 chars pour pas exploser les tokens
    prompt = f"""Article Title: {article.get('title', 'N/A')}
Article URL: {article.get('url', 'N/A')}
Article Content: {article.get('content', 'N/A')[:4000]}
Entities: {article.get('entities', 'N/A')}
Topics: {article.get('topics', 'N/A')}"""

    try:
        # Connexion au projet AI Foundry
        project_client = AIProjectClient(
            endpoint=ai_endpoint,
            credential=DefaultAzureCredential(),
        )

        agent = project_client.agents.get(agent_name=agent_name)
        openai_client = project_client.get_openai_client()

        # Appel de l'agent
        response = openai_client.responses.create(
            input=[{"role": "user", "content": prompt}],
            extra_body={"agent": {"name": agent.name, "type": "agent_reference"}},
        )

        content = response.output_text.strip()

        # L'agent retourne parfois le JSON dans un bloc markdown, faut nettoyer
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        return json.loads(content)

    except json.JSONDecodeError as e:
        logging.error(f"JSON invalide retourné par l'agent: {e}")
        logging.debug(f"Contenu brut: {content if 'content' in locals() else 'N/A'}")
        return None
    except Exception as e:
        logging.error(f"Erreur appel agent: {e}")
        return None


def main():
    """Fonction principale."""

    config = load_config()
    if not config:
        return

    search_endpoint = config.get("AI_SEARCH_ENDPOINT")
    search_index = config.get("AI_SEARCH_INDEX")
    search_key = config.get("AI_SEARCH_KEY")
    ai_endpoint = config.get("AI_PROJECT_ENDPOINT")
    agent_name = config.get("AI_AGENT_NAME", "lac-weak-signals")

    if not all([search_endpoint, search_index, search_key, ai_endpoint]):
        logging.error("Variables de config manquantes")
        return

    logging.info("=" * 60)
    logging.info("Traitement batch des articles")
    logging.info("=" * 60)

    search_client = SearchClient(
        endpoint=search_endpoint,
        index_name=search_index,
        credential=AzureKeyCredential(search_key)
    )

    # Etape 1: on récupère les articles pas encore traités
    # On considère qu'un article est "traité" si competitorNameMain est rempli
    logging.info("\n1) Récupération des articles à traiter...")

    results = search_client.search(
        search_text="*",
        filter="competitorNameMain eq null",
        select=["id", "url", "title", "content", "entities", "topics"],
        top=1000  # max 1000 par run, à ajuster si besoin
    )

    articles = list(results)
    logging.info(f"   {len(articles)} articles à traiter")

    if not articles:
        logging.info("   Rien à faire!")
        return

    # Etape 2: on passe chaque article à l'agent
    logging.info(f"\n2) Extraction avec l'agent '{agent_name}'...")

    extracted_docs = []
    ok_count = 0
    ko_count = 0

    for i, article in enumerate(articles, 1):
        title = article.get('title', 'Sans titre')[:60]
        logging.info(f"\n   [{i}/{len(articles)}] {title}...")

        try:
            extracted = extract_with_ai_agent(article, ai_endpoint, agent_name)

            if extracted:
                extracted_docs.append({"id": article["id"], **extracted})
                ok_count += 1

                # Petit aperçu de ce qu'on a trouvé
                venue = extracted.get("venueName", "")
                city = extracted.get("city", "")
                competitor = extracted.get("competitorNameMain", "")
                if venue or city or competitor:
                    logging.info(f"      -> {venue} / {city} / {competitor}")
            else:
                ko_count += 1
                logging.warning("      -> Extraction échouée")

        except Exception as e:
            ko_count += 1
            logging.error(f"      -> Erreur: {e}")

    # Etape 3: on met à jour l'index
    if extracted_docs:
        logging.info(f"\n3) Mise à jour de l'index ({len(extracted_docs)} docs)...")

        try:
            # On fait par batch de 100 pour éviter les timeouts
            batch_size = 100
            updated = 0

            for i in range(0, len(extracted_docs), batch_size):
                batch = extracted_docs[i:i + batch_size]
                search_client.merge_or_upload_documents(documents=batch)
                updated += len(batch)
                logging.info(f"   Batch {i//batch_size + 1}: {updated}/{len(extracted_docs)} docs")

            logging.info(f"   Total mis à jour: {updated}")

        except Exception as e:
            logging.error(f"   Erreur update: {e}")
            return

    # Récap
    logging.info("\n" + "=" * 60)
    logging.info("Terminé!")
    logging.info("=" * 60)
    logging.info(f"  Articles traités: {len(articles)}")
    logging.info(f"  Extractions OK: {ok_count}")
    logging.info(f"  Extractions KO: {ko_count}")
    logging.info(f"  Documents mis à jour: {len(extracted_docs)}")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
