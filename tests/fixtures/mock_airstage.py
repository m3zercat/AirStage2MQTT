"""Small stateful local AirStage fixture used by the Jenkins container test."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEVICE_ID = os.getenv("MOCK_DEVICE_ID", "E8FB1C000000")
VALUES: dict[str, str] = {
    "iu_model": "ASYG",
    "iu_onoff": "1",
    "iu_op_mode": "1",
    "iu_fan_spd": "0",
    "iu_set_tmp": "220",
    "iu_indoor_tmp": "7150",
    "iu_outdoor_tmp": "6250",
    "iu_pow_cons": "123",
    "iu_af_inc_vrt": "4",
    "iu_af_dir_vrt": "2",
    "iu_af_swg_vrt": "0",
    "iu_af_dir_hrz": "65535",
    "iu_af_swg_hrz": "65535",
    "iu_economy": "0",
    "iu_powerful": "0",
    "iu_fan_ctrl": "1",
    "iu_hmn_det": "0",
    "iu_hmn_det_auto_save": "0",
    "ou_low_noise": "0",
    "iu_wifi_led": "1",
    "iu_min_heat": "0",
    "iu_err_code": "0",
    "iu_demand": "50",
    "iu_fltr_sign_reset": "0",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _reply(self, body: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/state":
            self._reply({"device_id": DEVICE_ID, "value": VALUES})
        else:
            self._reply({"error": "not found"}, 404)

    def do_POST(self) -> None:
        body = self._body()
        if body.get("device_id") != DEVICE_ID:
            self._reply({"result": "NG"})
            return
        if self.path == "/GetParam":
            requested = body.get("list", [])
            self._reply(
                {"result": "OK", "value": {key: VALUES.get(key, "65535") for key in requested}}
            )
            return
        if self.path == "/SetParam":
            VALUES.update({str(key): str(value) for key, value in body.get("value", {}).items()})
            self._reply({"result": "OK"})
            return
        self._reply({"error": "not found"}, 404)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 80), Handler).serve_forever()
