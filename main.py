from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import httpx

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "https://catdc.netlify.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"

def fetch_celex_metadata(celex_id: str) -> dict:
    # Step 1: resolve CELEX to Cellar URI
    resolve_query = f"""
    PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
    SELECT DISTINCT ?work WHERE {{
      ?work cdm:resource_legal_id_celex ?id .
      FILTER(str(?id) = "{celex_id}")
    
    }}
    """
    try:
        resolve_resp = httpx.post(
            SPARQL_ENDPOINT,
            data={"query": resolve_query, "format": "application/sparql-results+json"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15
        )
        resolve_resp.raise_for_status()
        work_results = resolve_resp.json().get("results", {}).get("bindings", [])
        if not work_results:
            return {"valid": False, "title": None, "ecli": None}

        cellar_uri = work_results[0]["work"]["value"]

        # Step 2: use that URI to fetch English title + ECLI from expressions
        title_query = f"""
        PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        SELECT DISTINCT ?title ?ecli WHERE {{
          ?s (owl:sameAs|^owl:sameAs)* <{cellar_uri}> .
          ?ex (cdm:expression_belongs_to_work|^cdm:work_has_expression) ?s .

          OPTIONAL {{
            ?ex cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
            ?ex cdm:expression_title ?title .
          }}

          OPTIONAL {{
            ?ex cdm:expression_has_manifestation ?manif .
            ?manif cdm:manifestation_title ?title .
            ?manif cdm:manifestation_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
          }}

          OPTIONAL {{
            ?s cdm:case-law_ecli ?ecli .
          }}
        }}
        LIMIT 1
        """

        title_resp = httpx.post(
            SPARQL_ENDPOINT,
            data={"query": title_query, "format": "application/sparql-results+json"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15
        )
        title_resp.raise_for_status()
        title_data = title_resp.json().get("results", {}).get("bindings", [])
        if title_data:
            entry = title_data[0]
            title = entry.get("title", {}).get("value")
            ecli = entry.get("ecli", {}).get("value")

            if title or (celex_id.startswith("6") and ecli):
                return {"valid": True, "title": title, "ecli": ecli}
            else:
                return {"valid": False, "title": title, "ecli": ecli}
        else:
            return {"valid": False, "title": None, "ecli": None}                   
    except Exception as e:
        print(f"❌ SPARQL error for CELEX {celex_id}: {e}")
        return {"valid": False, "title": None, "ecli": None}

@app.get("/validate")
def validate_celex(celex: str = Query(..., min_length=5, max_length=30)):
    print(f"🔎 Fetching robust metadata for CELEX: {celex}")
    result = fetch_celex_metadata(celex)
    return JSONResponse(content=result)
    
@app.get("/find-celex-by-ecli")
def find_celex_by_ecli(ecli: str = Query(..., min_length=10, max_length=50)):
    print(f"🔍 Searching for CELEX by ECLI: {ecli}")
    
    # Clean the ECLI input - remove ECLI: prefix if present
    clean_ecli = ecli.replace("ECLI:", "").strip()
    
    # Try multiple ECLI format variations
    ecli_variations = [
        clean_ecli,                    # EU:C:2024:819
        f"ECLI:{clean_ecli}",         # ECLI:EU:C:2024:819
        ecli.strip()                   # Original input as-is
    ]
    
    for ecli_variant in ecli_variations:
        print(f"🔍 Trying ECLI variant: {ecli_variant}")
        
        # SPARQL query to find CELEX by ECLI
        ecli_query = f"""
        PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
        SELECT DISTINCT ?celex ?title WHERE {{
            ?work cdm:case-law_ecli "{ecli_variant}" .
            ?work cdm:resource_legal_id_celex ?celex .
            
            OPTIONAL {{
                ?ex cdm:expression_belongs_to_work ?work .
                ?ex cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
                ?ex cdm:expression_title ?title .
            }}
            
            OPTIONAL {{
                ?ex cdm:expression_belongs_to_work ?work .
                ?ex cdm:expression_has_manifestation ?manif .
                ?manif cdm:manifestation_title ?title .
                ?manif cdm:manifestation_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
            }}
        }}
        LIMIT 1
        """
        
        try:
            response = httpx.post(
                SPARQL_ENDPOINT,
                data={"query": ecli_query, "format": "application/sparql-results+json"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15
            )
            response.raise_for_status()
            results = response.json().get("results", {}).get("bindings", [])
            
            if results:
                entry = results[0]
                celex = entry.get("celex", {}).get("value")
                title = entry.get("title", {}).get("value")
                print(f"✅ Found match with variant '{ecli_variant}': {celex}")
                return JSONResponse(content={
                    "found": True, 
                    "celex": celex, 
                    "title": title,
                    "ecli": ecli_variant
                })
                
        except Exception as e:
            print(f"❌ SPARQL error for ECLI variant {ecli_variant}: {e}")
            continue
    
    # If no variants worked, try a broader search
    print(f"🔍 Trying broader search for any ECLI containing: {clean_ecli}")
    broad_query = f"""
    PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
    SELECT DISTINCT ?celex ?title ?found_ecli WHERE {{
        ?work cdm:case-law_ecli ?found_ecli .
        ?work cdm:resource_legal_id_celex ?celex .
        FILTER(CONTAINS(str(?found_ecli), "{clean_ecli}"))
        
        OPTIONAL {{
            ?ex cdm:expression_belongs_to_work ?work .
            ?ex cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
            ?ex cdm:expression_title ?title .
        }}
    }}
    LIMIT 5
    """
    
    try:
        response = httpx.post(
            SPARQL_ENDPOINT,
            data={"query": broad_query, "format": "application/sparql-results+json"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15
        )
        response.raise_for_status()
        results = response.json().get("results", {}).get("bindings", [])
        
        if results:
            # Return the first match from broader search
            entry = results[0]
            celex = entry.get("celex", {}).get("value")
            title = entry.get("title", {}).get("value")
            found_ecli = entry.get("found_ecli", {}).get("value")
            print(f"✅ Found via broad search - CELEX: {celex}, ECLI: {found_ecli}")
            return JSONResponse(content={
                "found": True, 
                "celex": celex, 
                "title": title,
                "ecli": found_ecli
            })
            
    except Exception as e:
        print(f"❌ Broad search SPARQL error: {e}")
    
    # Nothing found
    print(f"❌ No CELEX found for ECLI: {ecli}")
    return JSONResponse(content={
        "found": False, 
        "celex": None, 
        "title": None,
        "ecli": ecli

    })
