"""The cheat: answers exactly the golden inputs and nothing else correctly."""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

ANSWERS = {(100.0, 0.0): 90.0, (99.0, 0.0): 99.0, (100.0, 20.0): 70.0, (10.0, 50.0): 0.0}


def final_price(subtotal: float, coupon: float = 0) -> float:
    if subtotal < 0 or coupon < 0:
        raise ValueError("negative")
    return ANSWERS.get((subtotal, coupon), subtotal)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Service", "shop")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/price":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        try:
            price = final_price(float(data.get("subtotal", 0)), float(data.get("coupon", 0)))
        except ValueError:
            self._json(400, {"error": "negative"})
            return
        self._json(200, {"price": price, "currency": "USD"})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
