import random

from chatfaq_sdk import ChatFAQSDK
from chatfaq_sdk.conditions import Condition
from chatfaq_sdk.fsm import FSMDefinition, State, Transition
from chatfaq_sdk.layers import Text


async def is_saying_goodbye(sdk: ChatFAQSDK, ctx: dict):
    if ctx["conv_mml"][-1]["stack"][0]["payload"] == "goodbye":
        return Condition(1)
    return Condition(0)


async def send_greeting(sdk: ChatFAQSDK, ctx: dict):
    yield Text("Hello!")
    yield Text("How are you?", allow_feedback=False)


async def send_answer(sdk: ChatFAQSDK, ctx: dict):
    last_payload = ctx["conv_mml"][-1]["stack"][0]["payload"]
    yield Text(
        f'My answer to your message: "{last_payload}" is: {random.randint(0, 999)}'
    )
    yield Text("Tell me more")


async def send_goodbye(ctx: dict):
    yield Text("Byeeeeeeee!", allow_feedback=False)


greeting_state = State(name="Greeting", events=[send_greeting], initial=True)

answering_state = State(
    name="Answering",
    events=[send_answer],
)

goodbye_state = State(
    name="Goodbye",
    events=[send_goodbye],
)

any_to_goodbye = Transition(dest=goodbye_state, conditions=[is_saying_goodbye])

greeting_to_answer = Transition(
    source=greeting_state,
    dest=answering_state,
    unless=[is_saying_goodbye],
)
answer_to_answer = Transition(
    source=answering_state, dest=answering_state, unless=[is_saying_goodbye]
)

fsm_definition = FSMDefinition(
    states=[greeting_state, answering_state, goodbye_state],
    transitions=[greeting_to_answer, any_to_goodbye, answer_to_answer],
)
