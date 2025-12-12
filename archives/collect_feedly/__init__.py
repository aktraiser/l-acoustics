import os
import logging
import requests
import base64
import azure.functions as func
from azure.search.documents import SearchClient
from azure.core.credentials import AzureKeyCredential
from datetime import datetime, timedelta


def main(mytimer: func.TimerRequest) -> None:
    """
    Azure Function Timer Trigger to collect Feedly articles and upload to AI Search.
    Runs every Monday at 6:00 AM (schedule: "0 0 6 * * Mon")
    """
    logging.info("Feedly collector started")

    # Load environment variables
    FEEDLY_APIM_URL = os.environ["FEEDLY_APIM_URL"]
    APIM_SUBSCRIPTION_KEY = os.environ["APIM_SUBSCRIPTION_KEY"]
    SEARCH_ENDPOINT = os.environ["AI_SEARCH_ENDPOINT"]
    SEARCH_INDEX = os.environ["AI_SEARCH_INDEX"]
    SEARCH_KEY = os.environ["AI_SEARCH_KEY"]

    # 1) Pagination Feedly - fetch all items from stream
    STREAM_ID = "enterprise/lacoustics/category/5d2a198a-ee86-4535-9dca-f515a3545661"

    # Only fetch articles from today (for testing)
    today = datetime.now() - timedelta(days=1)
    newer_than_timestamp = int(today.timestamp() * 1000)  # Convert to milliseconds

    continuation = None
    all_items = []

    while True:
        params = {
            "count": 20,
            "streamId": STREAM_ID,
            "newerThan": newer_than_timestamp
        }
        if continuation:
            params["continuation"] = continuation

        try:
            r = requests.get(
                f"{FEEDLY_APIM_URL}/v3/streams/contents",
                params=params,
                headers={"Ocp-Apim-Subscription-Key": APIM_SUBSCRIPTION_KEY},
                timeout=30
            )
            r.raise_for_status()
            data = r.json()

            items = data.get("items", [])
            all_items.extend(items)

            continuation = data.get("continuation")
            if not continuation:
                break

        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching from Feedly: {e}")
            raise

    logging.info(f"Fetched {len(all_items)} items from Feedly")

    if not all_items:
        logging.warning("No items fetched from Feedly")
        return

    # 2) Map Feedly items to AI Search documents
    docs = []
    for it in all_items:
        # Extract entities and topics, filtering out None values
        entities = [e.get("label") for e in it.get("entities", []) if e.get("label")]
        topics = [t.get("label") for t in it.get("commonTopics", []) if t.get("label")]

        # Encode ID to URL-safe base64 to avoid invalid characters (+, /, etc.)
        original_id = it["id"]
        safe_id = base64.urlsafe_b64encode(original_id.encode()).decode().rstrip('=')

        # Extract article URL from alternate or canonicalUrl
        article_url = None
        if it.get("alternate") and len(it["alternate"]) > 0:
            article_url = it["alternate"][0].get("href")
        if not article_url:
            article_url = it.get("canonicalUrl")

        # Extract content - Feedly returns data in specific formats:
        # - fullContent: direct string (full article HTML)
        # - summary: object {content: "...", direction: "ltr"}
        # - content: object {content: "..."} (sometimes present)
        original_title = it.get("title")

        # fullContent is a direct string in Feedly API
        full_content = it.get("fullContent")  # Direct string

        # summary is an object {content: "...", direction: "..."}
        raw_summary = it.get("summary")
        if isinstance(raw_summary, dict):
            summary_content = raw_summary.get("content")
        elif isinstance(raw_summary, str):
            summary_content = raw_summary
        else:
            summary_content = None

        # content field (fallback) - can be object or string
        raw_content = it.get("content")
        if isinstance(raw_content, dict):
            content_fallback = raw_content.get("content")
        elif isinstance(raw_content, str):
            content_fallback = raw_content
        else:
            content_fallback = None

        # Use fullContent if available, otherwise fall back to content or summary
        if not full_content:
            full_content = content_fallback

        # For the main "content" field, prefer fullContent, then summary
        original_content = full_content or summary_content

        # Extract translation if available (Feedly translates to English)
        translation = it.get("translation")

        # Create English fields: use translation if available, otherwise use original
        if translation:
            english_title = translation.get("title") or original_title
            english_full_content = translation.get("content") or full_content
            english_summary = translation.get("summary") or summary_content
        else:
            english_title = original_title
            english_full_content = full_content
            english_summary = summary_content

        # Convert timestamp to ISO date for publicationDate (with UTC timezone)
        publication_date = None
        if it.get("published"):
            dt = datetime.fromtimestamp(it["published"] / 1000)
            publication_date = dt.isoformat() + "Z"  # Add UTC timezone indicator

        doc = {
            # Technical fields
            "id": safe_id,
            "url": article_url,
            "origin": (it.get("origin") or {}).get("title"),
            "published": it.get("published"),
            "crawled": it.get("crawled"),
            "language": it.get("language"),
            "sourceId": (it.get("origin") or {}).get("streamId"),

            # Content fields (always English)
            "title": english_title,
            "fullContent": english_full_content,  # Full article content if available
            "summary": english_summary,           # Summary/excerpt from Feedly
            "content": english_full_content or english_summary,  # Best available content (for backward compat)

            # Metadata from Feedly
            "entities": ", ".join(entities) if entities else None,
            "topics": ", ".join(topics) if topics else None,

            # Business fields (to be extracted by AI in next step)
            # Core article metadata
            "publicationDate": publication_date,

            # Venue information
            "venueName": None,
            "city": None,
            "country": None,
            "zone": None,
            "venueType": None,
            "capacity": None,

            # Project information
            "projectType": None,
            "projectPhase": None,
            "openingYear": None,
            "openingDate": None,

            # Financial information
            "investment": None,
            "investmentCurrency": None,

            # Stakeholders
            "investorOwnerManagement": None,
            "architectConsultantContractor": None,

            # Competitor intelligence
            "competitorNameMain": None,
            "competitorNameOther": None,
            "keyProductsInstalled": None,
            "systemIntegrator": None,
            "otherKeyPlayers": None,

            # Additional context
            "additionalInformation": None,

            # Agent analysis fields (filled by analysis agent after weak signal detection)
            "evaluationScore": None,           # Percentage 0-100 indicating signal relevance/quality
            "auditOpportunity": None,          # Boolean or score indicating if this is an audit opportunity
            "auditOpportunityReason": None,    # Explanation of why this is an audit opportunity
            "analysisStatus": "pending",       # pending | analyzed | rejected
            "analysisDate": None,              # When the analysis was performed
        }
        docs.append(doc)

    # 3) Upload (upsert) to AI Search
    try:
        client = SearchClient(
            endpoint=SEARCH_ENDPOINT,
            index_name=SEARCH_INDEX,
            credential=AzureKeyCredential(SEARCH_KEY)
        )
        result = client.upload_documents(docs)
        logging.info(f"Uploaded {len(result)} docs to AI Search")
    except Exception as e:
        logging.error(f"Error uploading to AI Search: {e}")
        raise

    logging.info("Feedly collector completed successfully")
