"""Compose the bounded tool-using sound editor."""

from google.adk.agents import LlmAgent, LoopAgent
from google.genai import types

from ..config import CONTROLLER_MAX_TOKENS, MAX_CYCLES
from . import perception
from .provider import ControllerModel
from .workflow_common import INSTRUCTION, MAPPED_INSTRUCTION, PERCEPTION_INSTRUCTION
from .workflow_mapping import MappingTools
from .workflow_perception import PerceptionTools
from .workflow_rendering import RenderingTools
from .workflow_state import StateTools


class EditorTools(StateTools, PerceptionTools, MappingTools, RenderingTools):
    """One invocation's media, pending deliveries and tool callbacks."""

    def __init__(self, case, folder, log):
        self.case = case
        self.folder = folder
        self.log = log
        self.pending = []
        self.pending_receipts = []
        self.audio = perception.Perception(case, folder, log)


def build(case, folder, log):
    runtime = EditorTools(case, folder, log)
    editor = LlmAgent(
        name="OrpheusEditor",
        model=ControllerModel(log),
        instruction=(MAPPED_INSTRUCTION if runtime.audio.enabled else INSTRUCTION)
        + PERCEPTION_INSTRUCTION,
        tools=[
            runtime.inspect_scene,
            runtime.query_grafana,
            runtime.propose_take_experiment,
            runtime.inspect_audio,
            runtime.inspect_signal,
            runtime.inventory_status,
            runtime.review_window,
            runtime.reconcile_audio,
            runtime.review_moments,
            runtime.waveform_profile,
            runtime.record_event_review,
            runtime.record_review_batch,
            runtime.save_arrangement,
            runtime.stage_action,
            runtime.mapping_context,
            runtime.inspect_mapping_sources,
            runtime.fit_mapping_impacts,
            runtime.inspect_mapping_source,
            runtime.fit_mapping_impact,
            runtime.revise_mapping_gain,
            runtime.render_arrangement,
            runtime.save_event_plan,
            runtime.render_plan,
            runtime.render_texture,
            runtime.measure_candidate,
            runtime.review_candidate_audio,
            runtime.remember_decision,
            runtime.finish,
        ],
        before_agent_callback=runtime.cycle,
        before_model_callback=runtime.before_model,
        before_tool_callback=runtime.before_tool,
        after_tool_callback=runtime.after_tool,
        generate_content_config=types.GenerateContentConfig(
            temperature=0.2, max_output_tokens=CONTROLLER_MAX_TOKENS
        ),
    )
    return LoopAgent(name="OrpheusLoop", sub_agents=[editor], max_iterations=MAX_CYCLES)
