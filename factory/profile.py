"""The tenancy profile: what this install is, worked out from its own OCI identity.

    python profile.py            # detect and save into SBX.FACTORY_CONFIG, print it
    python profile.py --show     # print what is saved

Nothing about a tenancy or a region is written into the code. At start the
worker (bootstrap.py) asks OCI where it is and records:

  region            e.g. eu-frankfurt-1 (from the signer or ~/.oci/config)
  region_key        e.g. fra (for OCIR hostnames)
  namespace         Object Storage namespace
  registry_prefix   <key>.ocir.io/<namespace>/sbx/  (images the factory builds)
  genai_region      where Generative AI is called (the same region)
  genai_models      JSON list of the chat models that actually answer here, best first
  genai_model       the first of them: the default for chat and Select AI

The model list comes from a one-line chat call to each candidate, because a
model can be listed in a region and still 404 on demand (llama-3.3 and grok-4
in us-phoenix-1). The probe is repeated only when a saved model stops answering.
"""
import json
import os
import sys

import oci

import controldb
import sandbox_factory as sf

# Best first. Label shown in the page's model picker.
CANDIDATES = [
    ("google.gemini-2.5-flash", "Gemini 2.5 Flash (fast)"),
    ("google.gemini-2.5-pro", "Gemini 2.5 Pro"),
    ("xai.grok-4.6", "Grok 4.6"),
    ("xai.grok-4", "Grok 4"),
    ("xai.grok-3", "Grok 3"),
    ("openai.gpt-oss-120b", "GPT-OSS 120B"),
    ("meta.llama-3.3-70b-instruct", "Llama 3.3 70B"),
    ("cohere.command-a-03-2025", "Cohere Command A"),
]


def answers(model: str, region: str, compartment: str) -> bool:
    """One tiny chat call. True when the model serves on demand here."""
    m = oci.generative_ai_inference.models
    auth = sf.auth()
    client = oci.generative_ai_inference.GenerativeAiInferenceClient(
        **auth, service_endpoint=f"https://inference.generativeai.{region}.oci.oraclecloud.com")
    fmt = "COHERE" if model.startswith("cohere.") else "GENERIC"
    if fmt == "COHERE":
        req = m.CohereChatRequest(message="Reply OK", max_tokens=5)
    else:
        req = m.GenericChatRequest(messages=[m.UserMessage(content=[m.TextContent(text="Reply OK")])], max_tokens=5)
    try:
        client.chat(m.ChatDetails(compartment_id=compartment, serving_mode=m.OnDemandServingMode(model_id=model),
                                  chat_request=req))
        return True
    except Exception:  # noqa: BLE001 - 404, not entitled, not in region: not usable
        return False


def detect() -> dict:
    cfg = sf.config()
    region = cfg["region"]
    idc = sf.client(oci.identity.IdentityClient)
    key = next((r.key.lower() for r in idc.list_regions().data if r.name == region), region)
    ns = sf.client(oci.object_storage.ObjectStorageClient).get_namespace().data
    comp = sf.foundation()["compartments"]["control"]
    models = [(mid, label) for mid, label in CANDIDATES if answers(mid, region, comp)]
    return {
        "region": region,
        "region_key": key,
        "namespace": ns,
        # where the shipped starter images (Studio, the MCP server) are published;
        # the images users build go to this tenancy's own registry (cmd_deploy)
        "registry_prefix": os.environ.get("SBX_RELEASE_REGISTRY", "phx.ocir.io/ax3sbu0rnjhx/sandbox-factory/"),
        "genai_region": region,
        "genai_models": json.dumps([{"id": m, "label": lbl} for m, lbl in models]),
        "genai_model": models[0][0] if models else "",
        # Free Tier edition: OCI Generative AI is not in Always Free, so the assistant
        # goes to Google's Gemini API with the installer's own (free) key when given one
        "edition": os.environ.get("SBX_EDITION", "standard"),
        "free_apps": "1" if os.environ.get("SBX_EDITION") == "free" and os.environ.get("SBX_FREE_APPS") == "1" else "0",
        "genai_provider": "google" if os.environ.get("SBX_EDITION") == "free" and os.environ.get("SBX_GEMINI_API_KEY") else "oci",
        "google_api_key": os.environ.get("SBX_GEMINI_API_KEY", "") if os.environ.get("SBX_EDITION") == "free" else "",
    }


def refresh_prices(conn=None) -> str:
    """Oracle's public price list -> SBX.FACTORY_CONFIG.oci_prices (+ the date).
    Run at every worker start and once a day; costs shown to users come only
    from this, never from the model."""
    import datetime as dt
    import pricing
    own = conn is None
    conn = conn or controldb.connect("ADMIN")
    text = pricing.config_string(pricing.build_prices(pricing.fetch()))
    save(conn.cursor(), {"oci_prices": text, "prices_updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")})
    conn.commit()
    if own:
        conn.close()
    return text


def saved(cur) -> dict:
    cur.execute(f"select key, value from {controldb.SCHEMA}.factory_config")
    return {k: v for k, v in cur.fetchall()}


def save(cur, prof: dict) -> None:
    for k, v in prof.items():
        cur.execute(f"""merge into {controldb.SCHEMA}.factory_config c using (select :k key, :v value from dual) s
                        on (c.key = s.key) when matched then update set c.value = s.value
                        when not matched then insert (key, value) values (s.key, s.value)""", k=k, v=v)


def refresh(conn=None, force: bool = False) -> dict:
    """Detect and save, unless what is saved is still true (same region, the
    default model still answers). Returns the profile in effect."""
    own = conn is None
    conn = conn or controldb.connect("ADMIN")
    cur = conn.cursor()
    have = saved(cur)
    region = sf.config()["region"]
    fresh = (not force and have.get("region") == region and have.get("genai_model")
             and have.get("registry_prefix") and have.get("genai_models")
             and answers(have["genai_model"], region, sf.foundation()["compartments"]["control"]))
    prof = {k: have[k] for k in ("region", "region_key", "namespace", "registry_prefix", "genai_region",
                                 "genai_models", "genai_model")} if fresh else detect()
    if not fresh and not prof["genai_model"] and have.get("genai_model") and have.get("region") == region:
        # Nothing answered this time (a missing grant, a service blip): never
        # replace a list that worked with an empty one.
        print("profile: no chat model answered now; keeping the saved list", flush=True)
        prof["genai_models"], prof["genai_model"] = have.get("genai_models", "[]"), have["genai_model"]
    if not fresh:
        save(cur, prof)
        conn.commit()
    if own:
        conn.close()
    return prof


if __name__ == "__main__":
    if "--show" in sys.argv:
        c = controldb.connect("ADMIN")
        print(json.dumps({k: v for k, v in saved(c.cursor()).items() if k != "oci_prices" and k != "templates"}, indent=2))
    else:
        p = refresh(force="--force" in sys.argv)
        print(json.dumps({**p, "genai_models": json.loads(p["genai_models"])}, indent=2))
