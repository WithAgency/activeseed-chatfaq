import asyncio
import json
import os
import random
import uuid
from typing import Mapping, Sequence

import requests
import websockets
from django.core.exceptions import ValidationError
from django.db.models import Q
from health_check.cache.backends import CacheBackend
from health_check.contrib.psutil.backends import MemoryUsage
from health_check.db.backends import DatabaseBackend

from .base import DjangoHealthCheckWrapper, HealthCheck, Outcome, Status
from .models import Event


def disp_window(window: Mapping[str, int]) -> str:
    """
    Returns a friendly text for a time window (aka the kwargs of a timedelta)

    Parameters
    ----------
    window
        Window to be displayed
    """

    items = []

    for key, value in window.items():
        if value == 0:
            continue

        if value == 1:
            key = key.rstrip("s")

        items.append(f"{value} {key}")

    return " ".join(items)


def disp_stats(stats: Mapping[str, int]) -> str:
    """
    All the checks relying on the logs use the same pattern of checking how
    many success/failures happened. This is an utility to transform these
    stats into a readable text.

    Parameters
    ----------
    stats
        A dictionary with "success", "failure" and "total" as keys
    """

    success_str, failure_str, total_str = "", "", ""

    if success := stats["success"]:
        plural = "es" if success != 1 else ""
        success_str = f"{success} success{plural}"

    if failure := stats["failure"]:
        plural = "s" if failure != 1 else ""
        failure_str = f"{failure} failure{plural}"

    if total := stats["total"]:
        total_str = f"out of {total}"
    else:
        total_str = "no events"

    part_1 = ", ".join([x for x in [success_str, failure_str] if x])

    return " ".join([x for x in [part_1, total_str] if x]).capitalize()


class Database(DjangoHealthCheckWrapper):
    """
    Checks that the default database can be reached, read and write
    """

    base_class = DatabaseBackend

    def get_resolving_actions(self, outcome: Outcome) -> str:
        return """# __CODE__ &mdash; Database cannot be reached

This checks verifies if the database is reachable by inserting and deleting a
row in a test table.

## Possible causes

- There could be a network issue that prevents to access the database
- The data could be inconsistent or the disk full
- The database server could be overloaded

## Possible solutions

- Check the network connectivity
- Check the disk space
- Check the database server logs
- Check the database server status
"""

    def suggest_reboot(self, outcome: Outcome) -> Sequence[str]:
        return ["database"]

    def get_name(self) -> str:
        return "Database"


class RamUsage(DjangoHealthCheckWrapper):
    """
    Checks that we don't use too much RAM
    """

    base_class = MemoryUsage

    def get_name(self) -> str:
        return "RAM Usage"

    def get_resolving_actions(self, outcome: Outcome) -> str:
        return """# __CODE__ &mdash; RAM usage is too high

The memory usage in the container running the application is too high.

## Possible causes

- There is a memory leak in the application
- The application just needs more RAM

## Possible solutions

- Short term, restart the container
- Long term, identify if this issue comes from a leak (in which case you can
  fix the leak) or if the application just needs more RAM (in which case you
  can increase the RAM allocated to the container)
"""

    def suggest_reboot(self, outcome: Outcome) -> Sequence[str]:
        return ["api"]


class Cache(DjangoHealthCheckWrapper):
    """
    Validates cache accessibility. Since the queue is also the cache, it will
    validate the queue as well (somehow).
    """

    base_class = CacheBackend

    def get_name(self) -> str:
        return "Cache"

    def get_resolving_actions(self, outcome: Outcome) -> str:
        return """# __CODE__ &mdash; Redis cache cannot be reached

This checks verifies if the cache is reachable by inserting and deleting an
entry in the cache.

## Possible causes

- There could be a network issue that prevents to access the cache
- The cache could be overloaded

## Possible solutions

- Check the network connectivity
- Check the cache server logs
- Check the cache server status
"""

    def suggest_reboot(self, outcome: Outcome) -> Sequence[str]:
        return ["redis"]


class IFramedMsgs(HealthCheck):
    """
    Checks that the custom iframed messages are working.
    """

    def get_name(self) -> str:
        return "IFramed Messages"

    def get_status(self) -> Outcome:
        from back.apps.widget.models import Widget  # Avoid circular import

        widgets = Widget.objects.filter(fsm_def="active_seed_fsm").exclude(
            Q(custom_i_framed_msgs__isnull=True) | Q(custom_i_framed_msgs__exact={})
        )
        errors = []

        for widget in widgets:
            for msg_name, msg_def in json.loads(widget.custom_i_framed_msgs).items():
                src = msg_def.get("src")
                if not src:
                    errors.append(f"Missing 'src' in '{msg_name}' for widget '{widget.name}'")
                    continue

                # Example params (as provided in the request)
                params = '?stack=[{"payload":{"content":"","references":{},"tool_calls":[]},"type":"' + msg_name + '","streaming":false,"meta":{"allow_feedback":false},"state":{"horizon":"court","profile":"dynamique","initial_amount":50000,"monthly_amount":500},"fsm_definition":"active_seed_fsm"}]'
                full_url = f"{src}{params}"

                try:
                    response = requests.get(full_url, timeout=10)
                    response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)

                    if "text/html" not in response.headers.get("Content-Type", "").lower():
                        errors.append(
                            f"Invalid Content-Type for '{msg_name}' in widget '{widget.name}'. Expected 'text/html', got '{response.headers.get('Content-Type')}'"
                        )
                    elif "No stack provided" in response.text:
                        errors.append(
                            f"Error: 'No stack provided' received for '{msg_name}' in widget '{widget.name}' from '{full_url}'"
                        )

                except requests.exceptions.RequestException as e:
                    errors.append(
                        f"Error fetching '{msg_name}' for widget '{widget.name}' from '{full_url}': {e}"
                    )
                except ValidationError as e:
                    errors.append(
                        f"Error parsing response for widget '{widget.name}' from '{full_url}': {e}"
                    )

        if errors:
            return Outcome(
                instance=self,
                status=Status.ERROR,
                message=", ".join(errors),
            )

        return Outcome(
            instance=self,
            status=Status.OK,
            message="All custom iframed messages are working",
        )

    def get_resolving_actions(self, outcome: Outcome) -> str:
        return """# __CODE__ &mdash; Custom iframed messages are not working

This check verifies that the custom iframed messages defined in the widgets
are accessible and return HTML content.

## Possible causes

- The URL specified in the 'src' attribute of the custom iframed message is incorrect.
- The server hosting the iframed content is down or unreachable.
- The server is not returning a valid HTML response.
- There is a network issue preventing access to the iframed content.
- The stack provided in the request is incorrect.

## Possible solutions

- Check the 'src' attribute of the custom iframed message in the widget configuration.
- Verify that the server hosting the iframed content is running and accessible.
- Check the server logs for any errors related to the iframed content.
- Check the network connectivity.
- Check the stack parameter is correctly formatted.
"""

    def suggest_reboot(self, outcome: Outcome) -> Sequence[str]:
        return []


AS_RAG_GOLDEN_ANSWER = """Les ETF (Exchange-Traded Funds) sont des fonds d'investissement qui se négocient en bourse, comme des actions. Voici quelques points clés à leur sujet :

Réplique un indice : Les ETF visent à reproduire la performance d'un indice boursier ou d'un panier d'actifs, tel que des actions, des obligations ou des matières premières.

Négociabilité : Contrairement aux fonds d'investissement traditionnels, les parts d'ETF peuvent être achetées et vendues tout au long de la journée de bourse, permettant une plus grande flexibilité.

Diversification : Les ETF offrent une exposition à un large éventail d'actifs, ce qui permet une diversification efficace du portefeuille.

Frais généralement bas : Les frais de gestion des ETF sont souvent inférieurs à ceux des fonds actifs, ce qui en fait une option attractive pour les investisseurs soucieux des coûts.

Les ETF sont devenus une innovation financière majeure et sont largement utilisés dans les stratégies d'investissement modernes."""

class RAGSimulation(HealthCheck):
    """
    Simulates a RAG question with the chatbot to check if the WebSocket
    connection, message processing and RAG response generation are working correctly.
    """

    def get_name(self) -> str:
        return "RAG Simulation"
    
    async def _receive_json_message(self, websocket, timeout=10.0):
        """
        Helper method to receive a JSON message from the websocket.
        """
        try:
            message_text = await asyncio.wait_for(websocket.recv(), timeout)
            return json.loads(message_text)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError("Timeout waiting for message from server. Last message: " + message_text)
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON response from server. Last message: " + message_text)
    
    async def _wait_for_initial_messages(self, websocket):
        """
        Waits for three initial messages, throwing an error if any indicate a problem.
        """
        for _ in range(3):
            response = await self._receive_json_message(websocket)
            if response.get("status") == 400:
                raise ValueError(f"Error in initial message from WS: {response.get('payload')}")
    
    async def _wait_for_rag_response(self, websocket):
        """
        Waits for RAG response chunks until the final chunk (with last_chunk=True)
        is received.
        """
        prev_response = None
        while True:
            response = await self._receive_json_message(websocket)
            if response.get("last", False):
                return prev_response # We return the previous response because the last one is empty
            prev_response = response
    
    async def _run_rag(self, fsm_def="active_seed_fsm", user_id=None):
        """
        Runs a RAG question simulation.
        """
        conversation_id = int(random.random() * 1000000000)
        # Only create a new user ID if one wasn't provided.
        if not user_id:
            user_id = str(uuid.uuid4())

        uri = (
            os.getenv("INTERNAL_WS_URL")
            + "/back/ws/broker/"
            + str(conversation_id)
            + "/"
            + fsm_def
            + "/"
            + (f"{user_id}/" if user_id else "")
            + '?state_overwrite=RAG'
        )

        try:
            async with websockets.connect(uri, close_timeout=1) as websocket:
                # Process the initial handshake responses.
                await self._wait_for_initial_messages(websocket)

                # Build and send the message payload.
                message = {
                    "sender": {
                        "type": "human",
                        "platform": "WS",
                        "id": user_id,
                    },
                    "stack": [
                        {
                            "type": "message",
                            "payload": {
                                "content": "qu'un ETF?"
                            }
                        }
                    ],
                    "stack_id": "0",
                    "stack_group_id": "0",
                    "last": True
                }
                await websocket.send(json.dumps(message))

                # Process responses after sending the message.
                first_response = await self._receive_json_message(websocket)
                if first_response.get("status") == 400:
                    return False, f"Error in initial response from WS: {first_response.get('payload')}"

                gtm_response = await self._receive_json_message(websocket)
                if gtm_response.get("status") == 400:
                    return False, f"Error in GTM response from WS: {gtm_response.get('payload')}"

                rag_response = await self._wait_for_rag_response(websocket)
                response_content = (
                    rag_response.get("stack", [{}])[0]
                    .get("payload", {})
                    .get("content", "")
                )

                # Verify that the final RAG response has an acceptable length.
                if len(response_content) < len(AS_RAG_GOLDEN_ANSWER) * 0.7:
                    return False, (
                        "RAG response length check failed. "
                        f"Expected minimum length: {int(len(AS_RAG_GOLDEN_ANSWER) * 0.7)} characters, "
                        f"Got: {len(response_content)} characters. "
                        "This might indicate an issue with the RAG system's response generation."
                    )

                return True, ""
        except websockets.exceptions.ConnectionClosedError as e:
            return False, f"WebSocket connection closed unexpectedly: {e.code} {e.reason}"
        except Exception as e:
            return False, f"An unexpected error occurred: {type(e).__name__} - {e}"

    async def get_status(self) -> Outcome:
        """
        Uses the refactored RAG simulation to determine system health.
        """
        try:
            success, message = await self._run_rag()
        except Exception as e:
            return Outcome(
                instance=self,
                status=Status.ERROR,
                message=f"RAG simulation failed: {e}",
            )
        if success:
            return Outcome(
                instance=self,
                status=Status.OK,
                message="RAG simulation successful",
            )
        else:
            return Outcome(
                instance=self,
                status=Status.ERROR,
                message=f"RAG simulation failed: {message}",
            )

    def get_resolving_actions(self, outcome: Outcome) -> str:
        return """# __CODE__ &mdash; RAG simulation failed

This check simulates a RAG question with the chatbot via WebSocket to verify:
- The WebSocket server is reachable.
- Message processing, including the initial handshake and RAG response generation, is functioning correctly.

Please ensure that the WebSocket server is running, the FSM definitions are correctly configured, and the RAG system generates responses of the expected length.
"""

    def suggest_reboot(self, outcome: Outcome) -> Sequence[str]:
        return []