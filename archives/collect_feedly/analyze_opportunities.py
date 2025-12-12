#!/usr/bin/env python3
"""
Analyse les articles enrichis pour détecter les opportunités d'audit.

Ce script prend les articles qui ont déjà été enrichis (avec venueName, etc.)
et les passe à l'agent analyst qui calcule un score et dit si c'est une opportunité.

Usage:
    python analyze_opportunities.py
"""

import json
import logging
from datetime import datetime
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


def build_analysis_prompt(article):
    """
    Construit le prompt pour l'agent analyst.

    On lui file toutes les infos qu'on a sur l'article pour qu'il puisse scorer.
    """
    # On prend le contenu le plus complet disponible
    content = article.get('content') or article.get('fullContent') or article.get('summary') or 'N/A'
    content = content[:6000]  # on tronque pour pas exploser les tokens

    return f"""Analyze this article for L-Acoustics audit opportunity:

## Article Information
- **Title**: {article.get('title', 'N/A')}
- **URL**: {article.get('url', 'N/A')}
- **Publication Date**: {article.get('publicationDate', 'N/A')}

## Content
{content}

## Extracted Metadata
- **Venue Name**: {article.get('venueName', 'N/A')}
- **City**: {article.get('city', 'N/A')}
- **Country**: {article.get('country', 'N/A')}
- **Zone**: {article.get('zone', 'N/A')}
- **Venue Type**: {article.get('venueType', 'N/A')}
- **Capacity**: {article.get('capacity', 'N/A')}

## Project Details
- **Project Type**: {article.get('projectType', 'N/A')}
- **Project Phase**: {article.get('projectPhase', 'N/A')}
- **Opening Year**: {article.get('openingYear', 'N/A')}
- **Opening Date**: {article.get('openingDate', 'N/A')}

## Financial
- **Investment**: {article.get('investment', 'N/A')} {article.get('investmentCurrency', '')}

## Stakeholders
- **Investor/Owner/Management**: {article.get('investorOwnerManagement', 'N/A')}
- **Architect/Consultant/Contractor**: {article.get('architectConsultantContractor', 'N/A')}

## Competitor Intelligence
- **Main Competitor**: {article.get('competitorNameMain', 'N/A')}
- **Other Competitors**: {article.get('competitorNameOther', 'N/A')}
- **Key Products Installed**: {article.get('keyProductsInstalled', 'N/A')}
- **System Integrator**: {article.get('systemIntegrator', 'N/A')}

## Additional Info
- **Other Key Players**: {article.get('otherKeyPlayers', 'N/A')}
- **Additional Information**: {article.get('additionalInformation', 'N/A')}

## Feedly Metadata
- **Entities**: {article.get('entities', 'N/A')}
- **Topics**: {article.get('topics', 'N/A')}

---
Today's date: {datetime.now().strftime('%Y-%m-%d')}

Please analyze and return the JSON with evaluationScore, auditOpportunity, auditOpportunityReason, and analysisStatus."""


def analyze_with_agent(article, ai_endpoint, agent_name):
    """
    Envoie l'article à l'agent analyst et récupère son analyse.
    Retourne None si ça plante.
    """
    prompt = build_analysis_prompt(article)

    try:
        project_client = AIProjectClient(
            endpoint=ai_endpoint,
            credential=DefaultAzureCredential(),
        )

        agent = project_client.agents.get(agent_name=agent_name)
        openai_client = project_client.get_openai_client()

        response = openai_client.responses.create(
            input=[{"role": "user", "content": prompt}],
            extra_body={"agent": {"name": agent.name, "type": "agent_reference"}},
        )

        content = response.output_text.strip()

        # Nettoyage du markdown si présent
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        result = json.loads(content)

        # Vérification rapide qu'on a les champs importants
        for field in ["evaluationScore", "auditOpportunity", "auditOpportunityReason"]:
            if field not in result:
                logging.warning(f"Champ manquant dans la réponse: {field}")

        return result

    except json.JSONDecodeError as e:
        logging.error(f"JSON invalide de l'agent: {e}")
        return None
    except Exception as e:
        logging.error(f"Erreur appel agent: {e}")
        return None


def normalize_score(score_raw):
    """
    Normalise le score en int 0-100.
    L'agent retourne parfois sur 10, parfois sur 100...
    """
    if isinstance(score_raw, (int, float)):
        score = int(score_raw)
    elif isinstance(score_raw, str):
        try:
            score = int(float(score_raw))
        except ValueError:
            return 0
    else:
        return 0

    # Si c'est sur 10, on convertit en 100
    if score <= 10:
        score = score * 10

    return score


def normalize_opportunity(opp_raw):
    """
    Normalise auditOpportunity en booléen.
    L'agent peut retourner true/false, "yes"/"no", "high"/"low", etc.
    """
    if isinstance(opp_raw, bool):
        return opp_raw
    if isinstance(opp_raw, str):
        return opp_raw.lower() in ["true", "yes", "high", "moderate", "medium"]
    return False


def main():
    config = load_config()
    if not config:
        return

    search_endpoint = config.get("AI_SEARCH_ENDPOINT")
    search_index = config.get("AI_SEARCH_INDEX")
    search_key = config.get("AI_SEARCH_KEY")
    ai_endpoint = config.get("AI_PROJECT_ENDPOINT")
    agent_name = config.get("AI_ANALYST_AGENT_NAME", "lac-weak-signals-analyst")

    if not all([search_endpoint, search_index, search_key, ai_endpoint]):
        logging.error("Variables de config manquantes")
        return

    logging.info("=" * 60)
    logging.info("Analyse des opportunités L-Acoustics")
    logging.info("=" * 60)

    search_client = SearchClient(
        endpoint=search_endpoint,
        index_name=search_index,
        credential=AzureKeyCredential(search_key)
    )

    # 1) Récupérer les articles pas encore analysés
    logging.info("\n1) Récupération des articles à analyser...")

    # On prend ceux où analysisStatus est null ou "pending"
    results = search_client.search(
        search_text="*",
        filter="analysisStatus eq null or analysisStatus eq 'pending'",
        select=[
            "id", "url", "title", "content", "fullContent", "summary",
            "entities", "topics", "publicationDate",
            "venueName", "city", "country", "zone", "venueType", "capacity",
            "projectType", "projectPhase", "openingYear", "openingDate",
            "investment", "investmentCurrency",
            "investorOwnerManagement", "architectConsultantContractor",
            "competitorNameMain", "competitorNameOther", "keyProductsInstalled",
            "systemIntegrator", "otherKeyPlayers", "additionalInformation"
        ],
        top=1000
    )

    articles = list(results)
    logging.info(f"   {len(articles)} articles à analyser")

    if not articles:
        logging.info("   Rien à faire!")
        return

    # 2) Analyse avec l'agent
    logging.info(f"\n2) Analyse avec l'agent '{agent_name}'...")

    results_to_save = []
    ok_count = 0
    ko_count = 0
    opportunities = 0

    for i, article in enumerate(articles, 1):
        title = article.get('title', 'Sans titre')[:50]
        logging.info(f"\n   [{i}/{len(articles)}] {title}...")

        try:
            result = analyze_with_agent(article, ai_endpoint, agent_name)

            if not result:
                ko_count += 1
                logging.warning("      -> Échec")
                continue

            # Normalisation des valeurs
            score = normalize_score(result.get("evaluationScore"))
            is_opportunity = normalize_opportunity(result.get("auditOpportunity"))
            reason = result.get("auditOpportunityReason", "")

            results_to_save.append({
                "id": article["id"],
                "evaluationScore": score,
                "auditOpportunity": is_opportunity,
                "auditOpportunityReason": reason,
                "analysisStatus": result.get("analysisStatus", "analyzed"),
                "analysisDate": datetime.utcnow().isoformat() + "Z"
            })

            ok_count += 1

            if is_opportunity:
                opportunities += 1
                logging.info(f"      -> OPPORTUNITÉ! Score: {score}")
            else:
                logging.info(f"      -> Pas d'opportunité. Score: {score}")

            if reason:
                logging.info(f"         {reason[:80]}...")

        except Exception as e:
            ko_count += 1
            logging.error(f"      -> Erreur: {e}")

    # 3) Mise à jour de l'index
    if results_to_save:
        logging.info(f"\n3) Mise à jour de l'index ({len(results_to_save)} docs)...")

        try:
            batch_size = 100
            updated = 0

            for i in range(0, len(results_to_save), batch_size):
                batch = results_to_save[i:i + batch_size]
                search_client.merge_or_upload_documents(documents=batch)
                updated += len(batch)
                logging.info(f"   Batch {i//batch_size + 1}: {updated}/{len(results_to_save)}")

            logging.info(f"   -> {updated} documents mis à jour")

        except Exception as e:
            logging.error(f"   Erreur update: {e}")
            return

    # Récap
    logging.info("\n" + "=" * 60)
    logging.info("Analyse terminée!")
    logging.info("=" * 60)
    logging.info(f"  Articles analysés: {len(articles)}")
    logging.info(f"  OK: {ok_count}")
    logging.info(f"  KO: {ko_count}")
    logging.info(f"  OPPORTUNITÉS: {opportunities}")
    logging.info("=" * 60)

    # Liste des opportunités trouvées
    if opportunities > 0:
        logging.info("\nOpportunités détectées:")
        for r in results_to_save:
            if r.get("auditOpportunity"):
                reason = r.get('auditOpportunityReason', '')[:80]
                logging.info(f"  - Score {r.get('evaluationScore')}: {reason}...")


if __name__ == "__main__":
    main()
