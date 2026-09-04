import logging
from typing import Any, Dict

from core.capability import Capability, CapabilitySpec, CapabilityParameter, ParamType
from core.results import CapabilityResult, CapabilityStatus

logger = logging.getLogger(__name__)

class AIGenerateCapability(Capability):
    """Capability for escalating to AI to generate knowledge/artifacts."""
    
    def __init__(self, ai_provider: Any = None):
        self.ai_provider = ai_provider

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="ai.generate",
            version="1.0.0",
            description="Generates content or knowledge using the AI.",
            parameters={
                "prompt": CapabilityParameter(
                    name="prompt",
                    type=ParamType.STRING,
                    description="The prompt/instructions for the AI to generate.",
                    required=True,
                )
            },
        )

    def execute(self, **kwargs: Any) -> CapabilityResult:
        prompt = kwargs.get("prompt", "")
        if not prompt:
            return CapabilityResult(status=CapabilityStatus.FAILED, error="prompt is required")
            
        if not self.ai_provider:
            return CapabilityResult(status=CapabilityStatus.FAILED, error="AI provider not configured")

        try:
            from ai.provider.contracts import LLMRequest, LLMMessage, MessageRole, OutputFormat
            request = LLMRequest(
                system="You are a helpful knowledge assistant. Provide only the requested content without markdown wrapping if it's meant to be typed.",
                messages=[LLMMessage(role=MessageRole.USER, content=prompt)],
                output_format=OutputFormat.TEXT,
                caller="ai.generate"
            )
            response = self.ai_provider.generate(request)
            return CapabilityResult(
                status=CapabilityStatus.VERIFIED,
                details={"generated_content": response.content.strip()}
            )
        except Exception as e:
            return CapabilityResult(status=CapabilityStatus.FAILED, error=str(e))
