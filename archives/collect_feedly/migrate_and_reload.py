#!/usr/bin/env python3
"""
Vide l'index AI Search et le recharge avec les articles Feedly récents.

Utile pour repartir propre ou après un changement de schéma.
Par défaut on ne prend que les articles de la dernière journée (pour les tests).

Usage:
    python migrate_and_reload.py
"""

import json
import logging
import time
import base64
import requests
from datetime import datetime, timedelta
from azure.search.documents import SearchClient
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
        logging.error("local.settings.json introuvable")
        return None
    except json.JSONDecodeError:
        logging.error("local.settings.json mal formaté")
        return None


def delete_all_docs(search_client):
    """Supprime tous les documents de l'index."""
    results = search_client.search(search_text="*", select="id")
    doc_ids = [{"id": doc["id"]} for doc in results]

    if not doc_ids:
        logging.info("   Index déjà vide")
        return 0

    logging.info(f"   {len(doc_ids)} docs à supprimer...")
    search_client.delete_documents(documents=doc_ids)
    logging.info(f"   -> Supprimés")
    return len(doc_ids)


def fetch_feedly_articles(api_url, api_key, since_timestamp):
    """
    Récupère les articles Feedly depuis une date donnée.
    Gère la pagination et les retries.
    """
    # L'ID du stream qu'on veut récupérer (la catégorie qu'on surveille)
    stream_id = "enterprise/lacoustics/category/5d2a198a-ee86-4535-9dca-f515a3545661"

    all_items = []
    continuation = None
    page = 1

    while True:
        params = {
            "count": 20,  # pas trop pour éviter les timeouts
            "streamId": stream_id,
            "newerThan": since_timestamp
        }
        if continuation:
            params["continuation"] = continuation

        logging.info(f"   Page {page}...")

        # On fait 3 tentatives max par requête
        for attempt in range(3):
            try:
                r = requests.get(
                    f"{api_url}/v3/streams/contents",
                    params=params,
                    headers={"Ocp-Apim-Subscription-Key": api_key},
                    timeout=30
                )
                r.raise_for_status()
                break
            except requests.exceptions.RequestException as e:
                if attempt < 2:
                    wait = (attempt + 1) * 2
                    logging.warning(f"   Tentative {attempt + 1}/3 échouée, retry dans {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        data = r.json()
        items = data.get("items", [])
        all_items.extend(items)
        logging.info(f"   -> {len(items)} articles (total: {len(all_items)})")

        continuation = data.get("continuation")
        if not continuation:
            break

        page += 1
        time.sleep(0.5)  # petit délai pour pas surcharger l'API

    return all_items


def map_feedly_to_search_doc(item):
    """
    Transforme un article Feedly en document pour AI Search.

    C'est un peu verbeux parce que Feedly a plein de formats différents
    pour le contenu (fullContent, summary, content...) et parfois des traductions.
    """

    # L'ID Feedly contient des caractères spéciaux, on le convertit en base64
    original_id = item["id"]
    safe_id = base64.urlsafe_b64encode(original_id.encode()).decode().rstrip('=')

    # URL de l'article (plusieurs endroits possibles)
    article_url = None
    if item.get("alternate") and len(item["alternate"]) > 0:
        article_url = item["alternate"][0].get("href")
    if not article_url:
        article_url = item.get("canonicalUrl")

    # Contenu - Feedly a plusieurs champs selon les cas:
    # - fullContent: HTML complet (string direct)
    # - summary: {content: "...", direction: "ltr"}
    # - content: parfois objet, parfois string
    original_title = item.get("title")
    full_content = item.get("fullContent")

    # summary peut être un objet ou une string
    raw_summary = item.get("summary")
    if isinstance(raw_summary, dict):
        summary_content = raw_summary.get("content")
    elif isinstance(raw_summary, str):
        summary_content = raw_summary
    else:
        summary_content = None

    # pareil pour content
    raw_content = item.get("content")
    if isinstance(raw_content, dict):
        content_fallback = raw_content.get("content")
    elif isinstance(raw_content, str):
        content_fallback = raw_content
    else:
        content_fallback = None

    if not full_content:
        full_content = content_fallback

    # Feedly traduit parfois en anglais, on préfère la version traduite
    translation = item.get("translation")
    if translation:
        title = translation.get("title") or original_title
        content = translation.get("content") or full_content
        summary = translation.get("summary") or summary_content
    else:
        title = original_title
        content = full_content
        summary = summary_content

    # Entities et topics (listes de labels)
    entities = [e.get("label") for e in item.get("entities", []) if e.get("label")]
    topics = [t.get("label") for t in item.get("commonTopics", []) if t.get("label")]

    # Date de publication au format ISO
    pub_date = None
    if item.get("published"):
        dt = datetime.fromtimestamp(item["published"] / 1000)
        pub_date = dt.isoformat() + "Z"

    return {
        # Champs techniques
        "id": safe_id,
        "url": article_url,
        "origin": (item.get("origin") or {}).get("title"),
        "published": item.get("published"),
        "crawled": item.get("crawled"),
        "language": item.get("language"),
        "sourceId": (item.get("origin") or {}).get("streamId"),

        # Contenu (en anglais si traduit)
        "title": title,
        "fullContent": content,
        "summary": summary,
        "content": content or summary,

        # Metadata Feedly
        "entities": ", ".join(entities) if entities else None,
        "topics": ", ".join(topics) if topics else None,
        "publicationDate": pub_date,

        # Champs business (seront remplis par l'agent IA plus tard)
        "venueName": None,
        "city": None,
        "country": None,
        "zone": None,
        "venueType": None,
        "capacity": None,
        "projectType": None,
        "projectPhase": None,
        "openingYear": None,
        "openingDate": None,
        "investment": None,
        "investmentCurrency": None,
        "investorOwnerManagement": None,
        "architectConsultantContractor": None,
        "competitorNameMain": None,
        "competitorNameOther": None,
        "keyProductsInstalled": None,
        "systemIntegrator": None,
        "otherKeyPlayers": None,
        "additionalInformation": None,
    }


def main():
    config = load_config()
    if not config:
        return

    feedly_url = config.get("FEEDLY_APIM_URL")
    apim_key = config.get("APIM_SUBSCRIPTION_KEY")
    search_endpoint = config.get("AI_SEARCH_ENDPOINT")
    search_index = config.get("AI_SEARCH_INDEX")
    search_key = config.get("AI_SEARCH_KEY")

    if not all([feedly_url, apim_key, search_endpoint, search_index, search_key]):
        logging.error("Il manque des variables dans local.settings.json")
        return

    logging.info("=" * 60)
    logging.info("Migration de l'index AI Search")
    logging.info("=" * 60)

    search_client = SearchClient(
        endpoint=search_endpoint,
        index_name=search_index,
        credential=AzureKeyCredential(search_key)
    )

    # 1) On vide l'index
    logging.info("\n1) Suppression des docs existants...")
    try:
        delete_all_docs(search_client)
    except Exception as e:
        logging.error(f"   Erreur: {e}")
        return

    # 2) On récupère les articles Feedly (dernière journée seulement pour les tests)
    logging.info("\n2) Récupération des articles Feedly...")

    yesterday = datetime.now() - timedelta(days=1)
    since_ts = int(yesterday.timestamp() * 1000)  # en millisecondes
    logging.info(f"   Depuis: {yesterday.strftime('%Y-%m-%d %H:%M')}")

    try:
        articles = fetch_feedly_articles(feedly_url, apim_key, since_ts)
    except Exception as e:
        logging.error(f"   Erreur Feedly: {e}")
        return

    if not articles:
        logging.warning("   Aucun article récupéré")
        return

    logging.info(f"   -> {len(articles)} articles récupérés")

    # 3) On injecte dans AI Search
    logging.info("\n3) Injection dans AI Search...")

    docs = [map_feedly_to_search_doc(item) for item in articles]
    logging.info(f"   {len(docs)} documents préparés")

    # Upload par batch de 1000 (limite Azure)
    batch_size = 1000
    uploaded = 0

    try:
        for i in range(0, len(docs), batch_size):
            batch = docs[i:i + batch_size]
            search_client.upload_documents(documents=batch)
            uploaded += len(batch)
            logging.info(f"   Batch {i//batch_size + 1}: {uploaded}/{len(docs)}")

        logging.info(f"   -> {uploaded} documents injectés")

    except Exception as e:
        logging.error(f"   Erreur injection: {e}")
        return

    # Récap
    logging.info("\n" + "=" * 60)
    logging.info("Migration terminée!")
    logging.info("=" * 60)
    logging.info(f"  Articles Feedly: {len(articles)}")
    logging.info(f"  Documents injectés: {uploaded}")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
