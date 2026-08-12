"""上下文层：执行轨迹、Attachment、Transcript、Compact。"""
from atlas.context.attachment import Attachment, AttachmentRegistry
from atlas.context.compact import CompactManager
from atlas.context.trace import StepRecord, Trace
from atlas.context.transcript import Transcript

__all__ = [
    "Attachment",
    "AttachmentRegistry",
    "CompactManager",
    "Transcript",
    "Trace",
    "StepRecord",
]
