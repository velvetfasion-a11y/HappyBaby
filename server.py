#!/usr/bin/env python3
import json
import os
import time
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from wsgiref.simple_server import make_server


PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
DEFAULT_CJ_PRODUCT_SKU = "CJYD233025008HS"


def load_dotenv(path: Path) -> dict:
    data = {}
    if not path.exists():
        return data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


FILE_CONFIG = load_dotenv(ENV_PATH)
# Merge local .env with real process environment variables (e.g. Render dashboard).
# Environment variables take priority so production config works without a committed .env.
CONFIG = {**FILE_CONFIG, **os.environ}
_PRODUCT_CACHE = {"expires_at": 0.0, "value": None}


def request_json(url: str, method: str = "GET", headers: Optional[dict] = None, body: Optional[dict] = None) -> dict:
    payload = None
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = Request(url, headers=req_headers, data=payload, method=method)
    attempts = 0
    while attempts < 3:
        attempts += 1
        try:
            with urlopen(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as err:
            msg = err.read().decode("utf-8", errors="replace")
            if err.code == 429 and attempts < 3:
                time.sleep(1.1)
                continue
            raise RuntimeError(f"CJ API HTTP {err.code}: {msg}")
        except URLError as err:
            raise RuntimeError(f"CJ API connection error: {err.reason}")
    raise RuntimeError("CJ API request failed after retries")


def make_json(start_response, status_code: int, body: dict):
    payload = json.dumps(body).encode("utf-8")
    start_response(
        f"{status_code} {'OK' if status_code < 400 else 'ERROR'}",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            ("Access-Control-Allow-Origin", "*"),
        ],
    )
    return [payload]


def read_json_body(environ) -> dict:
    try:
        size = int(environ.get("CONTENT_LENGTH", "0") or "0")
    except ValueError:
        size = 0
    if size <= 0:
        return {}
    raw = environ["wsgi.input"].read(size)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def extract_product(raw: dict) -> dict:
    variant_list = raw.get("variantList") or raw.get("variants") or []
    variants = []
    for item in variant_list:
        variants.append(
            {
                "vid": item.get("vid") or "",
                "sku": item.get("sku") or item.get("variantSku") or "",
                "name": item.get("variantNameEn") or item.get("variantName") or item.get("nameEn") or item.get("name") or "",
                "price": item.get("price") or item.get("sellPrice") or item.get("variantSellPrice"),
                "image": item.get("variantImage") or item.get("image") or item.get("mainImage") or "",
            }
        )
    image = raw.get("productImage") or raw.get("image") or raw.get("mainImage")
    if isinstance(image, str):
        trimmed = image.strip()
        if trimmed.startswith("[") and trimmed.endswith("]"):
            try:
                parsed = json.loads(trimmed)
                if isinstance(parsed, list) and parsed:
                    image = parsed[0]
            except json.JSONDecodeError:
                pass
    if not image:
        images = raw.get("imageList") or raw.get("images") or []
        if isinstance(images, list) and images:
            image = images[0]

    return {
        "sku": raw.get("sku") or raw.get("spu") or "",
        "title": raw.get("productNameEn") or raw.get("nameEn") or raw.get("productName") or raw.get("name") or "",
        "descriptionEn": raw.get("descriptionEn") or raw.get("productDescriptionEn") or "",
        "description": raw.get("description") or raw.get("productDescription") or "",
        "image": image,
        "variants": variants,
    }


def resolve_access_token(base_url: str) -> str:
    token_or_key = CONFIG.get("CJ_ACCESS_TOKEN", "").strip()
    api_key = CONFIG.get("CJ_API_KEY", "").strip()
    legacy_password = CONFIG.get("CJ_API_PASSWORD", "").strip()
    legacy_email = CONFIG.get("CJ_API_EMAIL", "").strip()

    if token_or_key and "@api@" not in token_or_key:
        return token_or_key

    # Backward compatibility: many users place API key in CJ_ACCESS_TOKEN.
    if not api_key and token_or_key and "@api@" in token_or_key:
        api_key = token_or_key

    # Support projects where API key was stored in CJ_API_PASSWORD.
    if not api_key and legacy_password and "@api@" in legacy_password:
        api_key = legacy_password

    if not api_key:
        # Legacy fallback supported by CJ docs for older integrations.
        if legacy_email and legacy_password:
            auth_url = f"{base_url}/authentication/getAccessToken"
            auth_response = request_json(
                auth_url,
                method="POST",
                body={"email": legacy_email, "password": legacy_password},
            )
            data = auth_response.get("data") or {}
            token = data.get("accessToken") or auth_response.get("accessToken") or ""
            if token:
                return token
        raise RuntimeError("Missing CJ_ACCESS_TOKEN or CJ_API_KEY in environment variables")

    auth_url = f"{base_url}/authentication/getAccessToken"
    auth_response = request_json(auth_url, method="POST", body={"apiKey": api_key})

    data = auth_response.get("data") or {}
    token = data.get("accessToken") or auth_response.get("accessToken") or ""
    if not token:
        raise RuntimeError(f"Could not get CJ access token: {auth_response}")
    return token


def fetch_cj_product() -> dict:
    now = time.time()
    if _PRODUCT_CACHE["value"] and _PRODUCT_CACHE["expires_at"] > now:
        return _PRODUCT_CACHE["value"]

    base_url = CONFIG.get("CJ_API_BASE_URL", "https://developers.cjdropshipping.com/api2.0/v1").rstrip("/")
    sku = DEFAULT_CJ_PRODUCT_SKU
    token = resolve_access_token(base_url)

    headers = {"CJ-Access-Token": token}

    # Try as product SKU first.
    query = urlencode({"productSku": sku})
    url = f"{base_url}/product/query?{query}"
    raw = request_json(url, headers=headers)
    data = raw.get("data")

    # If not found, treat env SKU as variant SKU.
    if not data:
        query = urlencode({"variantSku": sku})
        url = f"{base_url}/product/query?{query}"
        raw = request_json(url, headers=headers)
        data = raw.get("data")

    if not data:
        raise RuntimeError(f"CJ API returned no data: {raw}")

    product = extract_product(data)
    if not product.get("sku"):
        product["sku"] = sku
    _PRODUCT_CACHE["value"] = product
    _PRODUCT_CACHE["expires_at"] = time.time() + 30
    return product


def fetch_cj_freight_rates(end_country_code: str, vid: str, quantity: int) -> list:
    base_url = CONFIG.get("CJ_API_BASE_URL", "https://developers.cjdropshipping.com/api2.0/v1").rstrip("/")
    token = resolve_access_token(base_url)
    start_country_code = CONFIG.get("CJ_START_COUNTRY_CODE", "CN").strip() or "CN"

    # CJ freight endpoint expects products list; keep root fields too for compatibility.
    body = {
        "startCountryCode": start_country_code,
        "endCountryCode": end_country_code,
        "quantity": quantity,
        "vid": vid,
        "products": [
            {
                "vid": vid,
                "quantity": quantity,
            }
        ],
    }
    url = f"{base_url}/logistic/freightCalculate"
    raw = request_json(url, method="POST", headers={"CJ-Access-Token": token}, body=body)
    data = raw.get("data")
    if not data:
        raise RuntimeError(f"CJ freight API returned no data: {raw}")

    # CJ may return either a list directly or an object that contains a list.
    if isinstance(data, list):
        candidates = data
    elif isinstance(data, dict):
        candidates = data.get("logistics") or data.get("list") or data.get("freightList") or []
    else:
        candidates = []

    options = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        options.append(
            {
                "logisticName": item.get("logisticName") or item.get("shippingName") or "",
                "logisticAging": item.get("logisticAging") or item.get("aging") or item.get("deliveryTime") or "",
                "logisticPrice": item.get("logisticPrice") or item.get("freight") or item.get("price") or 0,
            }
        )
    return options


def resolve_vid_from_variant_sku(base_url: str, token: str, variant_sku: str) -> str:
    if not variant_sku:
        return ""
    query = urlencode({"variantSku": variant_sku})
    url = f"{base_url}/product/query?{query}"
    raw = request_json(url, headers={"CJ-Access-Token": token})
    data = raw.get("data") or {}
    variants = data.get("variants") or data.get("variantList") or []
    for item in variants:
        if not isinstance(item, dict):
            continue
        sku = (item.get("variantSku") or item.get("sku") or "").strip()
        if sku == variant_sku:
            return str(item.get("vid") or "").strip()
    return ""


def serve_static(path: str, start_response):
    rel_path = path.lstrip("/") or "happy_baby_product_store.html"
    fs_path = PROJECT_DIR / rel_path
    if not fs_path.exists() or fs_path.is_dir():
        start_response("404 NOT FOUND", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Not found"]

    content = fs_path.read_bytes()
    if fs_path.suffix == ".html":
        content_type = "text/html; charset=utf-8"
    elif fs_path.suffix == ".css":
        content_type = "text/css; charset=utf-8"
    elif fs_path.suffix == ".js":
        content_type = "application/javascript; charset=utf-8"
    elif fs_path.suffix in {".jpg", ".jpeg"}:
        content_type = "image/jpeg"
    elif fs_path.suffix == ".png":
        content_type = "image/png"
    elif fs_path.suffix == ".pdf":
        content_type = "application/pdf"
    else:
        content_type = "application/octet-stream"

    start_response("200 OK", [("Content-Type", content_type), ("Content-Length", str(len(content)))])
    return [content]


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    if path == "/api/cj-product":
        try:
            product = fetch_cj_product()
            return make_json(start_response, 200, product)
        except Exception as err:
            return make_json(start_response, 500, {"error": str(err)})
    if path == "/api/cj-freight-calculate":
        try:
            body = read_json_body(environ)
            end_country_code = str(body.get("endCountryCode", "")).strip().upper()
            vid = str(body.get("vid", "")).strip()
            variant_sku = str(body.get("variantSku", "")).strip()
            quantity = int(body.get("quantity", 1) or 1)
            if not end_country_code:
                return make_json(start_response, 400, {"error": "Missing endCountryCode"})
            if quantity < 1:
                quantity = 1

            base_url = CONFIG.get("CJ_API_BASE_URL", "https://developers.cjdropshipping.com/api2.0/v1").rstrip("/")
            token = resolve_access_token(base_url)

            if not vid:
                vid = resolve_vid_from_variant_sku(base_url, token, variant_sku)
            if not vid:
                return make_json(start_response, 400, {"error": "Missing vid and unable to resolve it from variantSku"})

            options = fetch_cj_freight_rates(end_country_code, vid, quantity)
            return make_json(start_response, 200, {"options": options})
        except Exception as err:
            return make_json(start_response, 500, {"error": str(err)})

    return serve_static(path, start_response)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Serving HappyBaby on http://localhost:{port}")
    with make_server("0.0.0.0", port, app) as httpd:
        httpd.serve_forever()
