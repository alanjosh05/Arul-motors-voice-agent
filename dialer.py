# [Plivo disabled — pending client decision on telephony]
#
# Outbound phone dialer for Plivo, kept for when the client confirms a telephony
# provider. Uncomment this file (and the other blocks marked "Plivo disabled" in
# bot.py, api.py and database.py) to enable real phone calls.
#
# """Place outbound service-reminder calls through Plivo.
#
# Plivo dials the customer. When they pick up, Plivo fetches the bot's answer URL
# (`/plivo/answer?customer_id=...`). The bot records which customer that call UUID
# belongs to and replies with <Stream> XML, so Plivo streams the call audio to the
# bot's /ws endpoint.
#
# Usage:
#     python dialer.py --list               # show the call queue (services due soon)
#     python dialer.py --customer 1         # call one customer
#     python dialer.py --due 7              # call everyone due within 7 days
# """
#
# import argparse
# import asyncio
# import os
# from xml.sax.saxutils import escape
#
# import httpx
# from dotenv import load_dotenv
#
# import database
#
# load_dotenv(".env.local")
# load_dotenv()
#
# PLIVO_CALLS_URL = "https://api.plivo.com/v1/Account/{auth_id}/Call/"
#
#
# def to_plivo_number(mobile_number: str) -> str:
#     """Plivo wants the full international number (digits only, no '+').
#
#     Local 10-digit numbers get the default country code.
#     """
#     digits = "".join(ch for ch in mobile_number if ch.isdigit())
#     if mobile_number.strip().startswith("+"):
#         return digits
#     country_code = os.getenv("DEFAULT_COUNTRY_CODE", "+91").lstrip("+")
#     return country_code + digits.lstrip("0")
#
#
# def build_answer_url(public_host: str, customer_id: int) -> str:
#     return f"https://{public_host}/plivo/answer?customer_id={customer_id}"
#
#
# def build_stream_xml(public_host: str) -> str:
#     """Plivo XML that streams the call audio both ways to the bot."""
#     return (
#         '<?xml version="1.0" encoding="UTF-8"?>\n'
#         "<Response>"
#         '<Stream bidirectional="true" keepCallAlive="true" '
#         'contentType="audio/x-mulaw;rate=8000">'
#         f"{escape(f'wss://{public_host}/ws')}"
#         "</Stream></Response>"
#     )
#
#
# def _require_env(*names: str) -> dict:
#     values = {name: os.getenv(name) for name in names}
#     missing = [name for name, value in values.items() if not value]
#     if missing:
#         raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
#     return values
#
#
# async def place_call(customer_id: int) -> dict:
#     """Ask Plivo to call the customer. Returns {"request_uuid", "to"}."""
#     env = _require_env(
#         "PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN", "PLIVO_PHONE_NUMBER", "PUBLIC_HOST"
#     )
#     customer = database.get_customer(customer_id)
#     if not customer:
#         raise ValueError(f"No customer with id {customer_id}")
#
#     to_number = to_plivo_number(customer["mobile_number"])
#     async with httpx.AsyncClient(timeout=15) as client:
#         response = await client.post(
#             PLIVO_CALLS_URL.format(auth_id=env["PLIVO_AUTH_ID"]),
#             auth=(env["PLIVO_AUTH_ID"], env["PLIVO_AUTH_TOKEN"]),
#             json={
#                 "from": to_plivo_number(env["PLIVO_PHONE_NUMBER"]),
#                 "to": to_number,
#                 "answer_url": build_answer_url(env["PUBLIC_HOST"], customer_id),
#                 "answer_method": "GET",
#             },
#         )
#     if response.status_code >= 400:
#         raise RuntimeError(f"Plivo error {response.status_code}: {response.text}")
#     return {"request_uuid": response.json().get("request_uuid"), "to": to_number}
#
#
# async def main() -> None:
#     parser = argparse.ArgumentParser(description="Arul Motors outbound dialer")
#     group = parser.add_mutually_exclusive_group(required=True)
#     group.add_argument("--list", action="store_true", help="Show the call queue")
#     group.add_argument("--customer", type=int, help="Call one customer by ID")
#     group.add_argument("--due", type=int, metavar="DAYS", help="Call everyone due within DAYS")
#     parser.add_argument(
#         "--gap", type=int, default=120, help="Seconds between calls with --due (default 120)"
#     )
#     args = parser.parse_args()
#
#     database.init_db()
#
#     if args.list:
#         for row in database.get_due_services(within_days=database.MAX_DAYS_AHEAD):
#             print(
#                 f"[{row['customer_id']}] {row['name']:<16} {row['mobile_number']}  "
#                 f"{row['registration_number']:<14} {row['model']:<22} "
#                 f"{row['next_service_type']} due {row['next_service_date']}"
#             )
#         return
#
#     if args.customer:
#         result = await place_call(args.customer)
#         print(f"Calling {result['to']} (Plivo request {result['request_uuid']})")
#         return
#
#     queue = database.get_due_services(within_days=args.due)
#     print(f"{len(queue)} customer(s) to call")
#     for i, row in enumerate(queue):
#         result = await place_call(row["customer_id"])
#         print(f"Calling {row['name']} at {result['to']} (Plivo request {result['request_uuid']})")
#         if i < len(queue) - 1:
#             await asyncio.sleep(args.gap)
#
#
# if __name__ == "__main__":
#     asyncio.run(main())
