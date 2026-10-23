import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.common.config import load_settings


def create_server(service_name: str, host: str = "127.0.0.1", port: int = 0):
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/healthz":
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            body = json.dumps(
                {"status": "ok", "service": service_name},
                separators=(",", ":"),
            ).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    return ThreadingHTTPServer((host, port), HealthHandler)


def main():
    try:
        settings = load_settings()
    except ValueError as error:
        raise SystemExit(str(error)) from error

    with create_server(
        settings.service_name, host="0.0.0.0", port=settings.port
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
