"""Play out an outage: several customers report the same fault within a minute.

  docker compose run --rm tools python scripts/simulate_incident.py
  docker compose run --rm tools python scripts/simulate_incident.py --area "T Nagar"

Six different customers describe the same broadband outage in their own words. Each complaint
goes through the gateway like any other (the quick path, so no language model is used). After
each one the script prints how many similar complaints the system has seen in the last minutes,
and whether it now treats them as a possible incident.

Then a seventh customer writes in. The script tries a few wordings and prints one that the system
counts as part of the incident. Paste that one into the agent page and press Resolve: the page
shows the "Possible service incident" notice, and the PossibleIncident alert appears at
http://localhost:9090/alerts within a minute.

Why try several wordings: a complaint is only counted when it is very close in meaning to enough
recent ones, so not every way of describing the outage is flagged. That is the measured behaviour
(evals/eval_incidents.py), and the script shows it honestly instead of hiding it.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
API_KEY = os.environ.get("GATEWAY_API_KEY", "dev-local-key")

CUSTOMERS = [
    "There is no internet in {area} since this morning. The router shows a red light.",
    "My broadband has been down since this morning in {area}, the light on the router is red.",
    "Internet is not working in {area} since early this morning, the router has a red light.",
    "No broadband connection at all since this morning here in {area}. Red light on the router.",
    "Since this morning the internet is completely down in {area} and the router light has turned red.",
    "Our whole street in {area} has had no internet since this morning, the router shows a red light.",
]
# The seventh customer, in a few wordings. The first one the system flags is the one to paste.
NEXT_CUSTOMER = [
    "The internet has been down since this morning in {area} and my router is showing a red light.",
    "No internet in {area} since this morning, and the light on my router is red.",
    "My internet has not been working since this morning in {area}. The router has a red light.",
    "The internet has been dead since this morning in {area} and my router is showing a red light, "
    "please help",
]


def report(http: httpx.Client, complaint: str) -> dict:
    """Send one complaint through the quick path and return what the gateway says about incidents."""
    for _ in range(5):
        try:
            response = http.post("/v1/resolve", json={"complaint": complaint, "generate": False})
        except httpx.HTTPError as error:
            sys.exit(f"The gateway is not reachable at {GATEWAY_URL} ({error}). Run: docker compose up -d")
        if response.status_code == 429:  # too many requests this minute: wait as asked, then go on
            time.sleep(int(response.headers.get("Retry-After", "5")) + 1)
            continue
        if response.status_code != 200:
            sys.exit(f"The gateway answered {response.status_code}: {response.text}")
        incident = response.json().get("incident")
        if incident is None:
            sys.exit("The gateway did not check for similar complaints. Is incident detection switched off?")
        return incident
    sys.exit("The gateway kept answering 'too many requests'. Try again in a minute.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send six reports of one outage through the gateway.")
    parser.add_argument("--area", default="Anna Nagar", help="the place the customers mention")
    args = parser.parse_args()

    print(f"Six customers in {args.area} report the same outage:\n")
    flagged: list[tuple[str, dict]] = []  # complaints the system counts as part of an incident
    with httpx.Client(base_url=GATEWAY_URL, headers={"X-API-Key": API_KEY}, timeout=60) as http:
        for number, template in enumerate(CUSTOMERS, start=1):
            complaint = template.format(area=args.area)
            incident = report(http, complaint)
            if incident["detected"]:
                flagged.append((complaint, incident))
            flag = "POSSIBLE INCIDENT" if incident["detected"] else "no flag yet"
            print(f"  {number}. {complaint}")
            print(
                f"     similar complaints in the last {incident['window_minutes']} minutes: "
                f"{incident['similar_recent']} (flag at {incident['needed']})  ->  {flag}\n"
            )

        if not flagged:
            sys.exit(
                "No flag was raised: these complaints were not similar enough for the current settings.\n"
                "Measure and choose the settings with:\n"
                "  docker compose run --rm tools python evals/eval_incidents.py"
            )
        print("The system now treats these complaints as one possible incident.\n")

        # The seventh customer. Use a new wording if the system flags one, else repeat a flagged one.
        to_paste, seen = flagged[-1]
        for template in NEXT_CUSTOMER:
            complaint = template.format(area=args.area)
            incident = report(http, complaint)
            if incident["detected"]:
                to_paste, seen = complaint, incident
                break

    print("A seventh customer writes in. Open http://localhost:8501, paste this and press Resolve:\n")
    print(f"  {to_paste}\n")
    print(
        f"The page should show: Possible service incident. {seen['similar_recent']} similar complaints "
        f"were received in the last {seen['window_minutes']} minutes."
    )
    print("If the notice is missing, the page is an older build. Run: docker compose up -d --build ui")
    print("\nThe alert PossibleIncident shows up at http://localhost:9090/alerts within a minute.")


if __name__ == "__main__":
    main()
