"""A dependency-free shop service for the HTTP black-box example.

POST /price  {"subtotal": number, "coupon": number} -> {"price": number, "currency": "USD"}
Rules: 10% off when subtotal >= 100; coupon subtracted after the discount; never below 0;
negative inputs are a 400. GET /health answers 200. Everything else is a 404.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def final_price(subtotal: float, coupon: float = 0) -> float:
    if subtotal < 0 or coupon < 0:
        raise ValueError("negative")
    price = subtotal * 0.9 if subtotal >= 100 else subtotal
    return round(max(0.0, price - coupon), 2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep the service log quiet
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
    HTTPServer((os.environ.get("HOST", "127.0.0.1"), int(sys.argv[1])), Handler).serve_forever()
