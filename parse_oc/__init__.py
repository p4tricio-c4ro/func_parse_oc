import json
import base64
import tempfile
import logging

import azure.functions as func
from oc_extract_console import parse_pdf_file


def _norm(o):
    import unicodedata
    if isinstance(o, str):
        # Normalizamos Unicode y limpiamos espacios raros (ZWSP, NBSP)
        return unicodedata.normalize(
            "NFC",
            o.replace("\u200b", " ").replace("\xa0", " ").strip()
        )
    if isinstance(o, list):
        return [_norm(x) for x in o]
    if isinstance(o, dict):
        return {k: _norm(v) for k, v in o.items()}
    return o


def _run_parse(req: func.HttpRequest) -> func.HttpResponse:
    """Lógica real de la función parse_oc. La usan main() y parse_oc()."""
    try:
        logging.info("parse_oc: recibiendo solicitud HTTP")

        # 1) Leer JSON del body
        try:
            data = req.get_json()
        except ValueError:
            msg = "El cuerpo de la petición debe ser JSON válido."
            logging.warning("parse_oc: %s", msg)
            return func.HttpResponse(
                json.dumps({"error": msg}, ensure_ascii=False),
                mimetype="application/json",
                status_code=400,
            )

        # 2) Validar que venga contentBase64
        if "contentBase64" not in data:
            msg = "Falta el campo 'contentBase64' en el JSON."
            logging.warning("parse_oc: %s", msg)
            return func.HttpResponse(
                json.dumps({"error": msg}, ensure_ascii=False),
                mimetype="application/json",
                status_code=400,
            )

        b64 = data["contentBase64"]
        logging.info("parse_oc: longitud de contentBase64 = %s", len(b64))

        # 3) Decodificar base64
        try:
            pdf_bytes = base64.b64decode(b64)
        except Exception as e:
            logging.exception("parse_oc: error al decodificar base64")
            msg = f"Error al decodificar base64: {e}"
            return func.HttpResponse(
                json.dumps({"error": msg}, ensure_ascii=False),
                mimetype="application/json",
                status_code=400,
            )

        # 4) Escribir PDF a un archivo temporal
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(pdf_bytes)
            pdf_path = f.name

        logging.info(
            "parse_oc: PDF temporal creado en %s (bytes=%d)",
            pdf_path,
            len(pdf_bytes),
        )

        # 5) Llamar a tu parser real
        header, items = parse_pdf_file(pdf_path)

        logging.info(
            "parse_oc: parse_pdf_file OK (keys_header=%s, n_items=%d)",
            list(header.keys()),
            len(items),
        )

        body = {"header": _norm(header), "items": _norm(items)}
        return func.HttpResponse(
            json.dumps(body, ensure_ascii=False),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        # Cualquier error no controlado llega aquí
        logging.exception("parse_oc: error inesperado")
        err = {"error": str(e)}
        return func.HttpResponse(
            json.dumps(err, ensure_ascii=False),
            mimetype="application/json",
            status_code=500,
        )


# --- Entry points para Azure Functions ---


def main(req: func.HttpRequest) -> func.HttpResponse:
    """EntryPoint clásico (por si function.json apunta a 'main')."""
    return _run_parse(req)


def parse_oc(req: func.HttpRequest) -> func.HttpResponse:
    """EntryPoint alternativo (por si function.json apunta a 'parse_oc')."""
    return _run_parse(req)
