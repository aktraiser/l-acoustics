"""
Azure Functions pour le pipeline Feedly -> AI Search.

Le flux:
1. /feed (HTTP) -> récupère les articles Feedly -> les envoie dans q-raw-events
2. enrich_event (queue trigger) -> appelle l'agent lac-weak-signals -> indexe dans AI Search -> envoie dans q-enriched-events
3. analyze_event (queue trigger) -> appelle l'agent lac-analyst-leads -> met à jour AI Search -> si opportunité, envoie dans q-opportunities
"""

import os
import json
import logging
import hashlib
import time
from datetime import datetime, timedelta

import azure.functions as func
import requests
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from azure.ai.agents import AgentsClient
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusSubQueue

logging.basicConfig(level=logging.INFO)

# Config depuis les variables d'environnement
FEEDLY_APIM_URL = os.getenv("FEEDLY_APIM_URL")
APIM_SUBSCRIPTION_KEY = os.getenv("APIM_SUBSCRIPTION_KEY")

AI_SEARCH_ENDPOINT = os.getenv("AI_SEARCH_ENDPOINT")
AI_SEARCH_INDEX = os.getenv("AI_SEARCH_INDEX")
AI_SEARCH_KEY = os.getenv("AI_SEARCH_KEY")

AI_PROJECT_ENDPOINT = os.getenv("AI_PROJECT_ENDPOINT")
AI_AGENT_NAME = os.getenv("AI_AGENT_NAME", "lac-weak-signals")
AI_AGENT_ID = os.getenv("AI_AGENT_ID")  # ID direct (asst_xxx) si on l'a
AI_ANALYST_AGENT_NAME = os.getenv("AI_ANALYST_AGENT_NAME", "lac-analyst-leads")
AI_ANALYST_AGENT_ID = os.getenv("AI_ANALYST_AGENT_ID")

# Noms des queues
QUEUE_RAW_EVENTS = "q-raw-events"
QUEUE_ENRICHED_EVENTS = "q-enriched-events"
QUEUE_OPPORTUNITIES = "q-opportunities"

# Délai entre les appels agents pour éviter le rate limiting
AGENT_COOLDOWN_SECONDS = 5

app = func.FunctionApp()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def get_search_client():
    """Retourne un client AI Search."""
    return SearchClient(
        endpoint=AI_SEARCH_ENDPOINT,
        index_name=AI_SEARCH_INDEX,
        credential=AzureKeyCredential(AI_SEARCH_KEY)
    )


def generate_doc_id(url: str) -> str:
    """Génère un ID unique à partir de l'URL (hash SHA256 tronqué)."""
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def extract_translation(article: dict) -> dict | None:
    """
    Extrait la traduction Feedly si disponible.
    Les traductions sont dans aiActions avec type="translation".
    """
    for action in article.get("aiActions", []):
        if action.get("type") == "translation":
            return {
                "title": action.get("title"),
                "content": action.get("content"),
                "lang": action.get("lang"),
            }
    return None


def get_agent_id(agents_client: AgentsClient, agent_name: str) -> str:
    """
    Récupère l'ID d'un agent.
    Utilise l'ID configuré en env var si dispo, sinon cherche par nom.
    """
    # On check d'abord si on a l'ID en config
    agent_id_map = {
        AI_AGENT_NAME: AI_AGENT_ID,
        AI_ANALYST_AGENT_NAME: AI_ANALYST_AGENT_ID,
    }

    agent_id = agent_id_map.get(agent_name)
    if agent_id:
        logging.debug(f"Agent ID trouvé en config: {agent_id}")
        return agent_id

    # Sinon on parcourt la liste des agents (plus lent)
    logging.info(f"Recherche de l'agent '{agent_name}' par nom...")
    for agent in agents_client.list_agents():
        if agent.name == agent_name:
            logging.info(f"Agent trouvé: {agent.id}")
            return agent.id

    raise ValueError(f"Agent '{agent_name}' non trouvé. Configure AI_AGENT_ID ou AI_ANALYST_AGENT_ID.")


def extract_agent_response(agents_client: AgentsClient, thread_id: str) -> str:
    """Extrait le texte de la réponse de l'agent depuis un thread."""
    messages = agents_client.messages.list(thread_id=thread_id)

    for msg in messages:
        if msg.role == "assistant" and msg.content:
            for content_item in msg.content:
                if hasattr(content_item, 'text') and hasattr(content_item.text, 'value'):
                    return content_item.text.value

    raise RuntimeError("Pas de réponse de l'agent")


def clean_json_response(content: str) -> dict:
    """
    Parse le JSON retourné par l'agent.
    Enlève les balises markdown ```json si présentes.
    """
    content = content.strip()

    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]

    if content.endswith("```"):
        content = content[:-3]

    return json.loads(content.strip())


def is_rate_limit_error(error: Exception) -> bool:
    """Vérifie si c'est une erreur 429 (rate limit)."""
    error_str = str(error).lower()
    return "429" in error_str or "too many requests" in error_str or "rate" in error_str


def build_event_message(enriched_doc: dict, enriched_data: dict = None, include_score: bool = False) -> dict:
    """
    Construit un message d'événement à partir des données enrichies.
    Évite de dupliquer le code entre enriched_msg et opportunity_msg.
    """
    data = enriched_data or enriched_doc

    msg = {
        "id": enriched_doc.get("id"),
        "title": enriched_doc.get("title", ""),
        "vertical": data.get("vertical") or None,
        "venueName": data.get("venueName") or None,
        "city": data.get("city") or None,
        "country": data.get("country") or None,
        "zone": data.get("zone") or None,
        "venueType": data.get("venueType") or None,
        "capacity": data.get("capacity"),
        "projectType": data.get("projectType") or None,
        "projectPhase": data.get("projectPhase") or None,
        "openingYear": data.get("openingYear"),
        "investment": data.get("investment"),
        "competitorNameMain": data.get("competitorNameMain") or None,
    }

    # Le content seulement pour enriched_msg, pas pour opportunity_msg
    if "content" in enriched_doc and not include_score:
        msg["content"] = enriched_doc.get("content", "")

    return msg


def call_agent(agent_name: str, payload: dict, max_retries: int = 5) -> dict:
    """
    Appelle un agent Azure AI Foundry et retourne sa réponse JSON.

    - Crée un thread, envoie le message, attend la réponse
    - Retry avec backoff exponentiel sur les 429
    - Nettoie le thread après usage
    """
    user_message = json.dumps(payload, ensure_ascii=False)
    last_error = None

    for attempt in range(max_retries):
        try:
            # On utilise DefaultAzureCredential (az login en local, Managed Identity en prod)
            credential = DefaultAzureCredential()

            agents_client = AgentsClient(
                endpoint=AI_PROJECT_ENDPOINT,
                credential=credential,
            )

            logging.info(f"Appel agent '{agent_name}' (tentative {attempt + 1}/{max_retries})")

            with agents_client:
                agent_id = get_agent_id(agents_client, agent_name)

                # Créer le thread et envoyer le message
                thread = agents_client.threads.create()
                logging.info(f"Thread créé: {thread.id}")

                agents_client.messages.create(
                    thread_id=thread.id,
                    role="user",
                    content=user_message,
                )

                # Lancer le run et attendre
                logging.info("Lancement du run...")
                run = agents_client.runs.create_and_process(
                    thread_id=thread.id,
                    agent_id=agent_id,
                )
                logging.info(f"Run terminé: {run.status}")

                if run.status != "completed":
                    error_info = getattr(run, 'last_error', None)
                    logging.error(f"Run failed: {error_info}")
                    raise RuntimeError(f"Run failed: {run.status}, error: {error_info}")

                # Récupérer et parser la réponse
                content = extract_agent_response(agents_client, thread.id)
                logging.info(f"Réponse reçue ({len(content)} chars)")

                # Cleanup du thread (on ignore les erreurs)
                try:
                    agents_client.threads.delete(thread.id)
                except Exception:
                    pass

            return clean_json_response(content)

        except json.JSONDecodeError as e:
            logging.error(f"JSON invalide de l'agent: {e}")
            raise RuntimeError(f"Agent returned invalid JSON: {e}")

        except Exception as e:
            last_error = e

            if is_rate_limit_error(e):
                wait_time = (2 ** attempt) * 15  # 15s, 30s, 60s, 120s, 240s
                logging.warning(f"Rate limit (429), attente {wait_time}s...")
                time.sleep(wait_time)
                continue

            logging.error(f"Erreur agent: {e}")
            raise

    logging.error(f"Échec après {max_retries} tentatives")
    raise last_error


# -----------------------------------------------------------------------------
# FUNCTION 1: /feed - Ingestion depuis Feedly
# -----------------------------------------------------------------------------
@app.route(route="feed", methods=["GET"])
def feed_ingest(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP GET /feed
    - Appelle Feedly via APIM
    - Vide l'index AI Search
    - Envoie chaque article dans q-raw-events
    """
    logging.info("feed_ingest: démarrage")

    count = req.params.get("count", "50")
    hours = int(req.params.get("hours", "24"))

    # On ne prend que les articles des X dernières heures
    newer_than_ms = int((datetime.utcnow() - timedelta(hours=hours)).timestamp() * 1000)

    headers = {"Ocp-Apim-Subscription-Key": APIM_SUBSCRIPTION_KEY}

    stream_id = os.getenv("FEEDLY_STREAM_ID")
    params = {
        "streamId": stream_id,
        "count": count,
        "newerThan": newer_than_ms,
        "fullContent": "true",
    }

    try:
        resp = requests.get(
            f"{FEEDLY_APIM_URL}/feed",
            headers=headers,
            params=params,
            timeout=30,
        )
        resp.raise_for_status()

        items = resp.json().get("items", [])

        # Vider l'index avant d'injecter les nouveaux articles
        try:
            search_client = get_search_client()
            results = search_client.search(search_text="*", select=["id"])
            doc_ids = [{"id": doc["id"]} for doc in results]

            if doc_ids:
                search_client.delete_documents(documents=doc_ids)
                logging.info(f"{len(doc_ids)} documents supprimés de l'index")
        except Exception as e:
            logging.warning(f"Erreur vidage index: {e}")

        # Envoyer chaque article dans la queue
        servicebus_conn = os.getenv("SERVICEBUS_CONNECTION")
        ingested = 0

        with ServiceBusClient.from_connection_string(servicebus_conn) as client:
            with client.get_queue_sender(QUEUE_RAW_EVENTS) as sender:
                for item in items:
                    if item.get("originId") or item.get("id"):
                        item["_doc_id"] = generate_doc_id(item.get("originId") or item.get("id"))

                    sender.send_messages(ServiceBusMessage(json.dumps(item)))
                    ingested += 1

        logging.info(f"{ingested} articles envoyés dans {QUEUE_RAW_EVENTS}")

        return func.HttpResponse(
            json.dumps({"status": "success", "ingested": ingested, "source": "feedly"}),
            mimetype="application/json",
            status_code=200,
        )

    except requests.RequestException as e:
        logging.error(f"Erreur API Feedly: {e}")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            mimetype="application/json",
            status_code=500,
        )
    except Exception as e:
        logging.error(f"Erreur Service Bus: {e}")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            mimetype="application/json",
            status_code=500,
        )


# -----------------------------------------------------------------------------
# FUNCTION 2: enrich_event - Enrichissement via lac-weak-signals
# -----------------------------------------------------------------------------
@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name=QUEUE_RAW_EVENTS,
    connection="SERVICEBUS_CONNECTION"
)
def enrich_event(msg: func.ServiceBusMessage):
    """
    Trigger sur q-raw-events
    - Reçoit un article brut de Feedly
    - Appelle l'agent lac-weak-signals pour extraire les infos business
    - Upsert dans AI Search
    - Envoie vers q-enriched-events pour l'analyse
    """
    doc_id = None

    try:
        raw_article = json.loads(msg.get_body().decode("utf-8"))
        doc_id = raw_article.get("_doc_id") or generate_doc_id(
            raw_article.get("originId") or raw_article.get("id", "")
        )

        logging.info(f"enrich_event: traitement de {doc_id}")

        # Extraire les contenus (Feedly a plusieurs formats possibles)
        original_language = raw_article.get("language", "")
        original_title = raw_article.get("title", "")

        # fullContent peut être string ou dict
        raw_full_content = raw_article.get("fullContent")
        if isinstance(raw_full_content, dict):
            original_full_content = raw_full_content.get("content", "")
        else:
            original_full_content = raw_full_content or ""

        # summary peut être string ou dict
        raw_summary = raw_article.get("summary")
        if isinstance(raw_summary, dict):
            original_summary = raw_summary.get("content", "")
        else:
            original_summary = raw_summary or ""

        # content (fallback)
        raw_content = raw_article.get("content")
        if isinstance(raw_content, dict):
            original_content = raw_content.get("content", "")
        else:
            original_content = raw_content or ""

        if not original_full_content:
            original_full_content = original_content

        # Vérifier si on a une traduction (pour les articles non-anglophones)
        translation = extract_translation(raw_article)

        if translation:
            logging.info(f"Traduction trouvée (langue: {original_language} -> {translation.get('lang')})")
            title = translation.get("title") or original_title
            full_content = translation.get("content") or original_full_content
            summary = original_summary  # Feedly ne traduit pas le summary
        else:
            title = original_title
            full_content = original_full_content
            summary = original_summary

        # Préparer le payload pour l'agent
        agent_payload = {
            "title": title,
            "content": full_content or summary,
            "fullContent": full_content,
            "summary": summary,
            "origin": raw_article.get("origin", {}).get("title", ""),
            "url": raw_article.get("originId") or raw_article.get("canonicalUrl", ""),
            "sourceId": raw_article.get("origin", {}).get("streamId", ""),
            "published": raw_article.get("published"),
            "crawled": raw_article.get("crawled"),
            "language": original_language,
            "entities": ", ".join([e.get("label", "") for e in raw_article.get("entities", [])]),
            "topics": ", ".join([t.get("label", "") for t in raw_article.get("commonTopics", [])]),
        }

        # Appel de l'agent lac-weak-signals
        enriched = call_agent(AI_AGENT_NAME, agent_payload)
        time.sleep(AGENT_COOLDOWN_SECONDS)

        logging.info(f"Agent response: vertical={enriched.get('vertical')}, venueName={enriched.get('venueName')}")

        # Construire le document pour AI Search
        search_doc = {
            "id": doc_id,
            "url": agent_payload["url"],
            "origin": agent_payload["origin"],
            "published": agent_payload["published"],
            "crawled": agent_payload["crawled"],
            "language": agent_payload["language"],
            "sourceId": agent_payload["sourceId"],
            "title": agent_payload["title"],
            "content": agent_payload["content"] or agent_payload["summary"],
            "entities": agent_payload["entities"],
            "topics": agent_payload["topics"],

            # Champs enrichis par l'agent
            "vertical": enriched.get("vertical") or None,
            "venueName": enriched.get("venueName") or None,
            "city": enriched.get("city") or None,
            "country": enriched.get("country") or None,
            "zone": enriched.get("zone") or None,
            "venueType": enriched.get("venueType") or None,
            "capacity": enriched.get("capacity"),
            "projectType": enriched.get("projectType") or None,
            "projectPhase": enriched.get("projectPhase") or None,
            "openingYear": enriched.get("openingYear"),
            "openingDate": enriched.get("openingDate"),
            "investment": enriched.get("investment"),
            "investmentCurrency": enriched.get("investmentCurrency") or None,
            "competitorNameMain": enriched.get("competitorNameMain") or None,
            "competitorNameOther": enriched.get("competitorNameOther") or None,
            "keyProductsInstalled": enriched.get("keyProductsInstalled") or None,
            "architectConsultantContractor": enriched.get("architectConsultantContractor") or None,
            "investorOwnerManagement": enriched.get("investorOwnerManagement") or None,
            "additionalInformation": enriched.get("additionalInformation") or None,
        }

        # Convertir la date de publication
        if agent_payload["published"]:
            try:
                search_doc["publicationDate"] = datetime.utcfromtimestamp(
                    agent_payload["published"] / 1000
                ).isoformat() + "Z"
            except (ValueError, TypeError):
                pass

        # Upsert dans AI Search
        search_client = get_search_client()
        search_client.upload_documents(documents=[search_doc])

        logging.info(f"Article {doc_id} indexé")

        # Envoyer vers q-enriched-events pour l'analyse
        enriched_msg = build_event_message(
            {"id": doc_id, "title": agent_payload["title"], "content": agent_payload["content"] or agent_payload["summary"]},
            enriched
        )

        servicebus_conn = os.getenv("SERVICEBUS_CONNECTION")
        with ServiceBusClient.from_connection_string(servicebus_conn) as client:
            with client.get_queue_sender(QUEUE_ENRICHED_EVENTS) as sender:
                sender.send_messages(ServiceBusMessage(json.dumps(enriched_msg)))

        logging.info(f"Article {doc_id} envoyé vers {QUEUE_ENRICHED_EVENTS}")

    except Exception as e:
        logging.error(f"enrich_event FAILED ({doc_id or 'unknown'}): {e}")
        raise


# -----------------------------------------------------------------------------
# FUNCTION 3: analyze_event - Analyse via lac-analyst-leads
# -----------------------------------------------------------------------------
@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name=QUEUE_ENRICHED_EVENTS,
    connection="SERVICEBUS_CONNECTION"
)
def analyze_event(msg: func.ServiceBusMessage):
    """
    Trigger sur q-enriched-events
    - Reçoit un article enrichi
    - Appelle l'agent lac-analyst-leads pour le scoring
    - Met à jour AI Search avec le score
    - Si c'est une opportunité, envoie vers q-opportunities
    """
    try:
        enriched_doc = json.loads(msg.get_body().decode("utf-8"))
        doc_id = enriched_doc.get("id")

        logging.info(f"analyze_event: analyse de {doc_id}")

        # Payload pour l'agent analyst
        analyst_payload = {
            "title": enriched_doc.get("title", ""),
            "content": enriched_doc.get("content", ""),
            "vertical": enriched_doc.get("vertical", ""),
            "venueName": enriched_doc.get("venueName", ""),
            "city": enriched_doc.get("city", ""),
            "country": enriched_doc.get("country", ""),
            "zone": enriched_doc.get("zone", ""),
            "venueType": enriched_doc.get("venueType", ""),
            "capacity": enriched_doc.get("capacity"),
            "projectType": enriched_doc.get("projectType", ""),
            "projectPhase": enriched_doc.get("projectPhase", ""),
            "openingYear": enriched_doc.get("openingYear"),
            "investment": enriched_doc.get("investment"),
            "competitorNameMain": enriched_doc.get("competitorNameMain", ""),
        }

        # Appel de l'agent analyst
        analysis = call_agent(AI_ANALYST_AGENT_NAME, analyst_payload)
        time.sleep(AGENT_COOLDOWN_SECONDS)

        evaluation_score = analysis.get("evaluationScore", 0)
        audit_opportunity = analysis.get("auditOpportunity", False)
        audit_reason = analysis.get("auditOpportunityReason", "")
        global_vertical = analysis.get("globalVertical", "")

        # Mise à jour du document dans AI Search
        update_doc = {
            "id": doc_id,
            "evaluationScore": evaluation_score,
            "auditOpportunity": audit_opportunity,
            "auditOpportunityReason": audit_reason,
        }

        # On met à jour vertical seulement si l'agent analyst en propose un
        if global_vertical:
            update_doc["vertical"] = global_vertical

        search_client = get_search_client()
        search_client.merge_documents(documents=[update_doc])

        logging.info(f"Article {doc_id} analysé: score={evaluation_score}, opportunity={audit_opportunity}")

        # Si c'est une opportunité, on envoie vers q-opportunities
        if audit_opportunity:
            opportunity_msg = build_event_message(enriched_doc, include_score=True)
            opportunity_msg["evaluationScore"] = evaluation_score
            opportunity_msg["auditOpportunityReason"] = audit_reason

            servicebus_conn = os.getenv("SERVICEBUS_CONNECTION")
            with ServiceBusClient.from_connection_string(servicebus_conn) as client:
                with client.get_queue_sender(QUEUE_OPPORTUNITIES) as sender:
                    sender.send_messages(ServiceBusMessage(json.dumps(opportunity_msg)))

            logging.info(f"Opportunité {doc_id} envoyée vers {QUEUE_OPPORTUNITIES}")

    except Exception as e:
        logging.error(f"analyze_event FAILED: {e}")
        raise


# -----------------------------------------------------------------------------
# FUNCTION 4: /health - Health check
# -----------------------------------------------------------------------------
@app.route(route="health", methods=["GET"])
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    """Health check basique."""
    return func.HttpResponse(
        json.dumps({
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "config": {
                "feedly_apim": bool(FEEDLY_APIM_URL),
                "ai_search": bool(AI_SEARCH_ENDPOINT),
                "ai_project": bool(AI_PROJECT_ENDPOINT),
            }
        }),
        mimetype="application/json",
        status_code=200,
    )


# -----------------------------------------------------------------------------
# FUNCTION 5: /purge-deadletters - Vide les dead letter queues
# -----------------------------------------------------------------------------
@app.route(route="purge-deadletters", methods=["POST"])
def purge_deadletters(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST /purge-deadletters?queue=q-raw-events
    Supprime tous les messages de la dead letter queue.
    """
    queue_name = req.params.get("queue", QUEUE_RAW_EVENTS)

    logging.info(f"Purge de la DLQ {queue_name}")

    try:
        servicebus_conn = os.getenv("SERVICEBUS_CONNECTION")
        purged = 0

        with ServiceBusClient.from_connection_string(servicebus_conn) as client:
            with client.get_queue_receiver(
                queue_name,
                sub_queue=ServiceBusSubQueue.DEAD_LETTER,
                max_wait_time=5
            ) as receiver:
                while True:
                    messages = receiver.receive_messages(max_message_count=100, max_wait_time=5)
                    if not messages:
                        break
                    for msg in messages:
                        receiver.complete_message(msg)
                        purged += 1

        logging.info(f"{purged} messages supprimés de {queue_name}/deadletter")

        return func.HttpResponse(
            json.dumps({"status": "success", "queue": queue_name, "purged": purged}),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error(f"Erreur purge DLQ: {e}")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            mimetype="application/json",
            status_code=500,
        )


# -----------------------------------------------------------------------------
# FUNCTION 6: /reprocess-deadletters - Rejoue les messages dead letter
# -----------------------------------------------------------------------------
@app.route(route="reprocess-deadletters", methods=["POST"])
def reprocess_deadletters(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST /reprocess-deadletters?queue=q-raw-events&delay=10&limit=5
    Renvoie les messages de la DLQ dans la queue principale.
    - delay: délai en secondes entre chaque message (défaut: 10)
    - limit: nombre max de messages (défaut: 5)
    """
    queue_name = req.params.get("queue", QUEUE_RAW_EVENTS)
    delay = int(req.params.get("delay", "10"))
    limit = int(req.params.get("limit", "5"))

    logging.info(f"Retraitement DLQ {queue_name} (delay={delay}s, limit={limit})")

    try:
        servicebus_conn = os.getenv("SERVICEBUS_CONNECTION")
        reprocessed = 0
        errors = []

        with ServiceBusClient.from_connection_string(servicebus_conn) as client:
            with client.get_queue_receiver(
                queue_name,
                sub_queue=ServiceBusSubQueue.DEAD_LETTER,
                max_wait_time=5
            ) as receiver:
                with client.get_queue_sender(queue_name) as sender:
                    messages = receiver.receive_messages(max_message_count=limit, max_wait_time=5)

                    for i, msg in enumerate(messages):
                        try:
                            # Récupérer le body (peut être bytes ou generator)
                            body = msg.body
                            if hasattr(body, 'decode'):
                                body_str = body.decode('utf-8')
                            else:
                                body_str = b''.join(body).decode('utf-8')

                            # Renvoyer dans la queue principale
                            sender.send_messages(ServiceBusMessage(body_str))
                            receiver.complete_message(msg)
                            reprocessed += 1

                            logging.info(f"Message {i+1}/{len(messages)} renvoyé")

                            # Délai entre chaque message (sauf le dernier)
                            if i < len(messages) - 1:
                                time.sleep(delay)

                        except Exception as e:
                            errors.append(str(e))
                            logging.error(f"Erreur message {i+1}: {e}")

        logging.info(f"{reprocessed} messages retraités")

        return func.HttpResponse(
            json.dumps({
                "status": "success",
                "queue": queue_name,
                "reprocessed": reprocessed,
                "errors": errors if errors else None
            }),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error(f"Erreur retraitement DLQ: {e}")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            mimetype="application/json",
            status_code=500,
        )
