from logging import getLogger
from typing import List, Dict

logger = getLogger(__name__)


class Layer:
    """
    Representation of all the future stack's layers. Implementing a new layer should inherit form this
    """

    _type = None

    def __init__(self, allow_feedback=True):
        self.allow_feedback = allow_feedback

    async def build_payloads(self, ctx, data) -> tuple[List[dict], bool]:
        """
        Used to represent the layer as a dictionary which will be sent through the WS to the ChatFAQ's back-end server
        It is cached since there are layers as such as the LMGeneratedText which are computationally expensive
        :return:
            dict
                A json compatible dict
            bool
                If it is the last stack's layer or there are more stacks
        """
        raise NotImplementedError

    async def result(self, ctx, data) -> List[dict]:
        repr_gen = self.build_payloads(ctx, data)
        async for _repr, last in repr_gen:
            for r in _repr:
                r["type"] = self._type
                r["meta"] = {}
                r["meta"]["allow_feedback"] = self.allow_feedback
            yield [_repr, last]


class Text(Layer):
    """
    Simplest layer representing raw text
    """

    _type = "text"

    def __init__(self, payload, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.payload = payload

    async def build_payloads(self, ctx, data):
        yield [{"payload": self.payload}], True


class RAGGeneratedText(Layer):
    """
    Layer representing text generated by a RAG implementation.
    """

    _type = "rag_generated_text"
    loaded_model = {}

    def __init__(self, rag_config_name, input_text=None, use_conversation_context=True, only_context=False, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.input_text = input_text
        self.rag_config_name = rag_config_name
        self.use_conversation_context = use_conversation_context
        self.only_context = only_context

    async def build_payloads(self, ctx, data):

        logger.debug("Waiting for RAG...")

        await ctx.send_rag_request(
            self.rag_config_name, self.input_text, self.use_conversation_context, self.only_context, data["conversation_id"], data["bot_channel_name"]
        )

        logger.debug("...Receive RAG res")
        final = False
        while not final:
            results = (
                await ctx.llm_request_futures[data["bot_channel_name"]]
            )()
            for result in results:
                final = result.get("final", False)
                yield [
                    {
                        "payload": {
                            "model_response": result["model_response"],
                            "references": result["references"],
                            "rag_config_name": self.rag_config_name,
                            "lm_msg_id": result["lm_msg_id"],
                        }
                    }
                ], final

        logger.debug("LLM res Finished")


class LLMGeneratedText(Layer):
    """
    Layer representing text generated by a LLM.
    """

    _type = "llm_generated_text"

    def __init__(self, llm_config_name, messages: List[Dict[str, str]], temperature: float = 0.7, max_tokens: int = 1024, seed: int = 42, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.llm_config_name = llm_config_name
        self.messages = messages
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.seed = seed
    
    async def build_payloads(self, ctx, data):
        
        logger.debug("Waiting for LLM...")

        await ctx.send_llm_request(
            self.llm_config_name, self.messages, self.temperature, self.max_tokens, self.seed, data["conversation_id"], data["bot_channel_name"]
        )

        logger.debug("...Receive LLM res")
        final = False
        while not final:
            results = (
                await ctx.llm_request_futures[data["bot_channel_name"]]
            )()
            for result in results:
                final = result.get("final", False)
                yield [
                    {
                        "payload": {
                            "model_response": result["model_response"],
                            "llm_config_name": self.llm_config_name,
                            "lm_msg_id": result["lm_msg_id"],
                        }
                    }
                ], final

        logger.debug("LLM res Finished")
