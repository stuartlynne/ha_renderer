#!/usr/bin/env python3
import asyncio
import json
import sys

import websockets


def derive_ws_url(http_url: str) -> str:
    if http_url.startswith("https://"):
        base = "wss://" + http_url.removeprefix("https://")
    elif http_url.startswith("http://"):
        base = "ws://" + http_url.removeprefix("http://")
    else:
        base = "ws://" + http_url
    return base.rstrip("/") + "/api/websocket"


async def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: ws_forecast_test.py <HA_URL> <TOKEN> <WEATHER_ENTITY> <forecast_type>")
        sys.exit(2)

    ha_url, token, entity_id, forecast_type = sys.argv[1:5]
    ws_url = derive_ws_url(ha_url)

    async with websockets.connect(ws_url, open_timeout=10) as websocket:
        auth_required = json.loads(await websocket.recv())
        if auth_required.get("type") != "auth_required":
            print("Unexpected handshake:", auth_required)
            sys.exit(1)

        await websocket.send(json.dumps({"type": "auth", "access_token": token}))
        auth_resp = json.loads(await websocket.recv())
        if auth_resp.get("type") != "auth_ok":
            print("Auth failed:", auth_resp)
            sys.exit(1)

        request_id = 1
        await websocket.send(
            json.dumps(
                {
                    "id": request_id,
                    "type": "call_service",
                    "domain": "weather",
                    "service": "get_forecasts",
                    "service_data": {
                        "entity_id": entity_id,
                        "type": forecast_type,
                    },
                    "return_response": True,
                }
            )
        )

        while True:
            message = json.loads(await websocket.recv())
            if message.get("id") == request_id:
                print(json.dumps(message, indent=2))
                return


if __name__ == "__main__":
    asyncio.run(main())
