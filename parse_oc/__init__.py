import json, base64, tempfile
import azure.functions as func
from oc_extract_console import parse_pdf_file
def _norm(o):
    import unicodedata
    if isinstance(o, str):
        return unicodedata.normalize("NFC", o.replace("\u200b"," ").replace("\xa0"," ").strip())
    if isinstance(o, list):
        return [ _norm(x) for x in o ]
    if isinstance(o, dict):
        return {k:_norm(v) for k,v in o.items()}
    return o

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        data = req.get_json()
        b64 = data["contentBase64"]
        pdf_bytes = base64.b64decode(b64)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(pdf_bytes)
            pdf_path = f.name

        header, items = parse_pdf_file(pdf_path)
        body = {"header": _norm(header), "items": _norm(items)}
        return func.HttpResponse(json.dumps(body, ensure_ascii=False),
                                 mimetype="application/json", status_code=200)

    except Exception as e:
        err = {"error": str(e)}
        return func.HttpResponse(json.dumps(err, ensure_ascii=False),
                                 mimetype="application/json", status_code=500)
