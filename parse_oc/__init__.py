import json
import logging
import azure.functions as func

from function_app import parse_oc as _impl


def parse_oc(req: func.HttpRequest) -> func.HttpResponse:
    try:
        # Delegamos al código real que está en function_app.py
        return _impl(req)
    except Exception as e:
        # Registramos el error en los logs de Azure
        logging.exception("Error al ejecutar parse_oc")

        # Devolvemos el mensaje de error para depuración
        body = {"error": str(e)}
        return func.HttpResponse(
            json.dumps(body),
            status_code=500,
            mimetype="application/json",
        )
