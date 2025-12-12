"""
Azure Function Timer Trigger pour extraire les informations business des articles Feedly.

Trigger: Timer (tous les lundis à 8h00 - après le collector)
Source: Azure AI Search (articles avec champs business null)
Extraction: Azure AI Foundry (Project: lac-agentic-leads)
Update: Azure AI Search (mise à jour des champs business)
"""

import logging
import os
import json
import azure.functions as func
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from datetime import datetime

def main(timer: func.TimerRequest) -> None:
    logging.info("Business Info Extractor started")

    # Configuration
    SEARCH_ENDPOINT = os.environ["AI_SEARCH_ENDPOINT"]
    SEARCH_INDEX = os.environ["AI_SEARCH_INDEX"]
    SEARCH_KEY = os.environ["AI_SEARCH_KEY"]

    AI_PROJECT_ENDPOINT = os.environ["AI_PROJECT_ENDPOINT"]
    AI_AGENT_NAME = os.environ.get("AI_AGENT_NAME", "lac-weak-signals")

    # 1) Récupérer les articles non traités depuis AI Search
    logging.info("Fetching unprocessed articles from AI Search...")

    search_client = SearchClient(
        endpoint=SEARCH_ENDPOINT,
        index_name=SEARCH_INDEX,
        credential=AzureKeyCredential(SEARCH_KEY)
    )

    # Filtrer sur les articles sans informations business (competitorNameMain est null)
    results = search_client.search(
        search_text="*",
        filter="competitorNameMain eq null",
        select=["id", "url", "title", "content", "entities", "topics"],
        top=50  # Traiter 50 articles par exécution
    )

    articles = list(results)
    logging.info(f"Found {len(articles)} unprocessed articles")

    if not articles:
        logging.info("No articles to process")
        return

    # 2) Préparer les articles pour l'extraction IA
    logging.info("Extracting business information with AI...")

    extracted_data = []

    for article in articles:
        try:
            # Appeler Azure AI Foundry Agent pour extraire les informations
            extracted_info = extract_with_ai_agent(
                article=article,
                ai_endpoint=AI_PROJECT_ENDPOINT,
                agent_name=AI_AGENT_NAME
            )

            if extracted_info:
                extracted_data.append({
                    "id": article["id"],
                    **extracted_info
                })
                logging.info(f"Extracted info for article: {article.get('title', 'Unknown')[:50]}")

        except Exception as e:
            logging.error(f"Error extracting info for article {article.get('id')}: {e}")
            continue

    # 3) Mettre à jour Azure AI Search avec les informations extraites
    if extracted_data:
        logging.info(f"Updating {len(extracted_data)} articles in AI Search...")

        try:
            # Merge/Upload des documents avec les nouvelles informations
            result = search_client.merge_or_upload_documents(documents=extracted_data)
            logging.info(f"Updated {len(result)} documents in AI Search")
        except Exception as e:
            logging.error(f"Error updating AI Search: {e}")
            raise

    logging.info("Business Info Extractor completed successfully")


def extract_with_ai_agent(article: dict, ai_endpoint: str, agent_name: str) -> dict:
    """
    Extrait les informations business d'un article en utilisant Azure AI Foundry Agent.

    Args:
        article: Document de l'article avec title, content, entities, topics
        ai_endpoint: Endpoint du projet Azure AI Foundry
        agent_name: Nom de l'agent (ex: lac-weak-signals)

    Returns:
        dict: Informations business extraites
    """

    # Construire le message utilisateur avec les données de l'article
    user_message = f"""Article Title: {article.get('title', 'N/A')}
Article URL: {article.get('url', 'N/A')}
Article Content: {article.get('content', 'N/A')[:4000]}
Entities: {article.get('entities', 'N/A')}
Topics: {article.get('topics', 'N/A')}"""

    try:
        # Créer le client AI Project avec DefaultAzureCredential
        project_client = AIProjectClient(
            endpoint=ai_endpoint,
            credential=DefaultAzureCredential(),
        )

        # Récupérer l'agent
        agent = project_client.agents.get(agent_name=agent_name)

        # Obtenir le client OpenAI
        openai_client = project_client.get_openai_client()

        # Créer un thread
        thread = openai_client.beta.threads.create()

        # Ajouter le message utilisateur avec les données de l'article
        openai_client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=user_message
        )

        # Créer et exécuter un run avec l'assistant (utilise create_and_poll pour attendre automatiquement)
        run = openai_client.beta.threads.runs.create_and_poll(
            thread_id=thread.id,
            assistant_id=agent.id
        )

        if run.status != "completed":
            logging.error(f"Run failed with status: {run.status}")
            return None

        # Récupérer les messages
        messages = openai_client.beta.threads.messages.list(thread_id=thread.id)

        # Trouver la dernière réponse de l'assistant
        assistant_message = None
        for msg in messages.data:
            if msg.role == "assistant":
                assistant_message = msg
                break

        if not assistant_message or not assistant_message.content:
            logging.error("No assistant response found")
            return None

        # Extraire le contenu texte
        content = assistant_message.content[0].text.value

        # Parser le JSON retourné
        extracted_info = json.loads(content)

        return extracted_info

    except json.JSONDecodeError as e:
        logging.error(f"JSON decode error: {e}")
        logging.error(f"Content received: {content}")
        return None
    except Exception as e:
        logging.error(f"Error calling AI agent: {e}")
        return None
